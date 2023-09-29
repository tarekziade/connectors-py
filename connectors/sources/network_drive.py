#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""Network Drive source module responsible to fetch documents from Network Drive.
"""
import asyncio
import os
from functools import cached_property, partial
from io import BytesIO

import fastjsonschema
import smbclient
import winrm
from smbprotocol.exceptions import SMBException, SMBOSError
from smbprotocol.file_info import (
    InfoType,
)
from smbprotocol.open import (
    DirectoryAccessMask,
    FilePipePrinterAccessMask,
    SMB2QueryInfoRequest,
    SMB2QueryInfoResponse,
)
from smbprotocol.security_descriptor import (
    SMB2CreateSDBuffer,
)
from wcmatch import glob

from connectors.access_control import (
    ACCESS_CONTROL,
    es_access_control_query,
    prefix_identity,
)
from connectors.filtering.validation import (
    AdvancedRulesValidator,
    SyncRuleValidationResult,
)
from connectors.source import BaseDataSource
from connectors.utils import (
    TIKA_SUPPORTED_FILETYPES,
    RetryStrategy,
    get_base64_value,
    iso_utc,
    retryable,
)

ACCESS_ALLOWED_TYPE = 0
ACCESS_MASK_DENIED_WRITE_PERMISSION = 278
GET_USERS_COMMAND = "Get-LocalUser | Select Name, SID"
GET_GROUPS_COMMAND = "Get-LocalGroup | Select-Object Name, SID"
GET_GROUP_MEMBERS = 'Get-LocalGroupMember -Name "{name}" | Select-Object Name, SID'
SECURITY_INFO_DACL = 0x00000004

MAX_CHUNK_SIZE = 65536
DEFAULT_FILE_SIZE_LIMIT = 10485760
RETRIES = 3
RETRY_INTERVAL = 2


def _prefix_user(user):
    return prefix_identity("user", user)


def _prefix_sid(sid):
    return prefix_identity("sid", sid)


class InvalidRulesError(Exception):
    pass


class NetworkDriveAdvancedRulesValidator(AdvancedRulesValidator):
    RULES_OBJECT_SCHEMA_DEFINITION = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    SCHEMA_DEFINITION = {"type": "array", "items": RULES_OBJECT_SCHEMA_DEFINITION}
    SCHEMA = fastjsonschema.compile(definition=SCHEMA_DEFINITION)

    def __init__(self, source):
        self.source = source

    async def validate(self, advanced_rules):
        if len(advanced_rules) == 0:
            return SyncRuleValidationResult.valid_result(
                SyncRuleValidationResult.ADVANCED_RULES
            )

        return await self._remote_validation(advanced_rules)

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )
    async def _remote_validation(self, advanced_rules):
        try:
            NetworkDriveAdvancedRulesValidator.SCHEMA(advanced_rules)
        except fastjsonschema.JsonSchemaValueException as e:
            return SyncRuleValidationResult(
                rule_id=SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=e.message,
            )

        self.source.create_connection()

        _, invalid_rules = self.source.find_matching_paths(advanced_rules)

        if len(invalid_rules) > 0:
            return SyncRuleValidationResult(
                SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=f"Following patterns do not match any path'{', '.join(invalid_rules)}'",
            )

        return SyncRuleValidationResult.valid_result(
            SyncRuleValidationResult.ADVANCED_RULES
        )


class SecurityInfo:
    def __init__(self, user, password, server):
        self.username = user
        self.server_ip = server
        self.password = password

    def get_descriptor(self, file_descriptor, info):
        """Get the Security Descriptor for the opened file."""
        query_request = SMB2QueryInfoRequest()
        query_request["info_type"] = InfoType.SMB2_0_INFO_SECURITY
        query_request["output_buffer_length"] = MAX_CHUNK_SIZE
        query_request["additional_information"] = info
        query_request["file_id"] = file_descriptor.file_id

        req = file_descriptor.connection.send(
            query_request,
            sid=file_descriptor.tree_connect.session.session_id,
            tid=file_descriptor.tree_connect.tree_connect_id,
        )
        response = file_descriptor.connection.receive(req)
        query_response = SMB2QueryInfoResponse()
        query_response.unpack(response["data"].get_value())

        security_descriptor = SMB2CreateSDBuffer()
        security_descriptor.unpack(query_response["buffer"].get_value())

        return security_descriptor

    @cached_property
    def session(self):
        return winrm.Session(
            self.server_ip,
            auth=(self.username, self.password),
            transport="ntlm",
            server_cert_validation="ignore",
        )

    def parse_output(self, raw_output):
        formatted_result = {}
        output_lines = raw_output.std_out.decode().splitlines()

        #  Ignoring initial headers with fixed length of 2
        if len(output_lines) > 2:
            for line in output_lines[3:]:
                parts = line.rsplit(maxsplit=1)
                if len(parts) == 2:
                    key, value = parts
                    key = key.strip()
                    value = value.strip()
                    formatted_result[key] = value

        return formatted_result

    def fetch_users(self):
        users = self.session.run_ps(GET_USERS_COMMAND)
        return self.parse_output(users)

    def fetch_groups(self):
        groups = self.session.run_ps(GET_GROUPS_COMMAND)

        return self.parse_output(groups)

    def fetch_members(self, group_name):
        members = self.session.run_ps(GET_GROUP_MEMBERS.format(name=group_name))

        return self.parse_output(members)


class NASDataSource(BaseDataSource):
    """Network Drive"""

    name = "Network Drive"
    service_type = "network_drive"
    advanced_rules_enabled = True
    dls_enabled = True

    def __init__(self, configuration):
        """Set up the connection to the Network Drive

        Args:
            configuration (DataSourceConfiguration): Object of DataSourceConfiguration class.
        """
        super().__init__(configuration=configuration)
        self.username = self.configuration["username"]
        self.password = self.configuration["password"]
        self.server_ip = self.configuration["server_ip"]
        self.port = self.configuration["server_port"]
        self.drive_path = self.configuration["drive_path"]
        self.session = None
        self.security_info = SecurityInfo(self.username, self.password, self.server_ip)

    def advanced_rules_validators(self):
        return [NetworkDriveAdvancedRulesValidator(self)]

    @classmethod
    def get_default_configuration(cls):
        """Get the default configuration for Network Drive.

        Returns:
            dictionary: Default configuration.
        """
        return {
            "username": {
                "label": "Username",
                "order": 1,
                "type": "str",
            },
            "password": {
                "label": "Password",
                "order": 2,
                "sensitive": True,
                "type": "str",
            },
            "server_ip": {
                "label": "SMB IP",
                "order": 3,
                "type": "str",
            },
            "server_port": {
                "display": "numeric",
                "label": "SMB port",
                "order": 4,
                "type": "int",
            },
            "drive_path": {
                "label": "SMB path",
                "order": 5,
                "type": "str",
            },
            "use_document_level_security": {
                "display": "toggle",
                "label": "Enable document level security",
                "order": 6,
                "tooltip": "Document level security ensures identities and permissions set in Network Drive are maintained in Elasticsearch. This enables you to restrict and personalize read-access users and groups have to documents in this index. Access control syncs ensure this metadata is kept up to date in your Elasticsearch documents.",
                "type": "bool",
                "value": False,
            },
        }

    def create_connection(self):
        """Creates an SMB session to the shared drive."""
        self.session = smbclient.register_session(
            server=self.server_ip,
            username=self.username,
            password=self.password,
            port=self.port,
        )

    @cached_property
    def get_directory_details(self):
        return list(smbclient.walk(top=rf"\\{self.server_ip}/{self.drive_path}"))

    def find_matching_paths(self, advanced_rules):
        """
        Find matching paths based on advanced rules.

        Args:
            advanced_rules (list): List of advanced rules configured

        Returns:
            matched_paths (set): Set of paths that match the advanced rules.
            invalid_rules (list): List of advanced rules that have no matching paths.
        """
        invalid_rules = []
        matched_paths = set()
        for rule in advanced_rules:
            rule_valid = False
            glob_pattern = rule["pattern"].replace("\\", "/")
            for path, _, _ in self.get_directory_details:
                normalized_path = path.split("/", 1)[1].replace("\\", "/")
                is_match = glob.globmatch(
                    normalized_path, glob_pattern, flags=glob.GLOBSTAR
                )

                if is_match:
                    rule_valid = True
                    matched_paths.add(path)
            if not rule_valid:
                invalid_rules.append(rule["pattern"])
        return matched_paths, invalid_rules

    async def ping(self):
        """Verify the connection with Network Drive"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor=None, func=self.create_connection)
        self._logger.info("Successfully connected to the Network Drive")

    async def close(self):
        """Close all the open smb sessions"""
        if self.session is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            executor=None,
            func=partial(
                smbclient.delete_session, server=self.server_ip, port=self.port
            ),
        )

    async def get_files(self, path):
        """Fetches the metadata of the files and folders present on given path

        Args:
            path (str): The path of a folder in the Network Drive
        """
        files = []
        loop = asyncio.get_running_loop()
        try:
            files = await loop.run_in_executor(None, smbclient.scandir, path)
        except (SMBOSError, SMBException) as exception:
            self._logger.exception(
                f"Error while scanning the path {path}. Error {exception}"
            )

        for file in files:
            file_details = file._dir_info.fields
            yield {
                "path": file.path,
                "size": file_details["allocation_size"].get_value(),
                "_id": file_details["file_id"].get_value(),
                "created_at": iso_utc(file_details["creation_time"].get_value()),
                "_timestamp": iso_utc(file_details["change_time"].get_value()),
                "type": "folder" if file.is_dir() else "file",
                "title": file.name,
            }

    def fetch_file_content(self, path):
        """Fetches the file content from the given drive path

        Args:
            path (str): The file path of the file on the Network Drive
        """
        try:
            with smbclient.open_file(
                path=path, encoding="utf-8", errors="ignore", mode="rb"
            ) as file:
                file_content, chunk = BytesIO(), True
                while chunk:
                    chunk = file.read(MAX_CHUNK_SIZE) or b""
                    file_content.write(chunk)
                file_content.seek(0)
                return file_content
        except SMBOSError as error:
            self._logger.error(
                f"Cannot read the contents of file on path:{path}. Error {error}"
            )

    async def get_content(self, file, timestamp=None, doit=None):
        """Get the content for a given file

        Args:
            file (dictionary): Formatted file document
            timestamp (timestamp, optional): Timestamp of file last modified. Defaults to None.
            doit (boolean, optional): Boolean value for whether to get content or not. Defaults to None.

        Returns:
            dictionary: Content document with id, timestamp & text
        """
        if not (
            doit
            and (os.path.splitext(file["title"])[-1]).lower()
            in TIKA_SUPPORTED_FILETYPES
            and file["size"]
        ):
            return

        if int(file["size"]) > DEFAULT_FILE_SIZE_LIMIT:
            self._logger.warning(
                f"File size {file['size']} of {file['title']} bytes is larger than {DEFAULT_FILE_SIZE_LIMIT} bytes. Discarding the file content"
            )
            return

        loop = asyncio.get_running_loop()
        content = await loop.run_in_executor(
            executor=None, func=partial(self.fetch_file_content, path=file["path"])
        )

        attachment = content.read()
        content.close()
        return {
            "_id": file["id"],
            "_timestamp": file["_timestamp"],
            "_attachment": get_base64_value(content=attachment),
        }

    def list_file_permission(self, file_path, file_type, mode, access):
        with smbclient.open_file(
            file_path,
            mode=mode,
            buffering=0,
            file_type=file_type,
            desired_access=access,
        ) as file:
            descriptor = self.security_info.get_descriptor(
                file_descriptor=file.fd, info=SECURITY_INFO_DACL
            )
            return descriptor.get_dacl()["aces"]

    def _dls_enabled(self):
        if (
            self._features is None
            or not self._features.document_level_security_enabled()
        ):
            return False

        return self.configuration["use_document_level_security"]

    async def _decorate_with_access_control(self, document, file_path, file_type):
        if self._dls_enabled():
            entity_permissions = await self.get_entity_permission(
                file_path=file_path, file_type=file_type
            )
            document[ACCESS_CONTROL] = list(
                set(document.get(ACCESS_CONTROL, []) + entity_permissions)
            )
        return document

    async def _user_access_control_doc(self, user, sid, groups_info, groups_members):
        prefixed_username = _prefix_user(user)
        sid_users = _prefix_sid(sid)
        sid_groups = []

        for group_name, group_sid in groups_info.items():
            members = groups_members.get(group_name)

            if sid in members.values():
                sid_groups.append(_prefix_sid(group_sid))

        access_control = [sid_users, prefixed_username, *sid_groups]

        return {
            "_id": sid,
            "identity": {
                "username": prefixed_username,
                "user_id": sid_users,
            },
            "created_at": iso_utc(),
        } | es_access_control_query(access_control)

    async def get_access_control(self):
        if not self._dls_enabled():
            self._logger.warning("DLS is not enabled. Skipping")
            return

        self._logger.info("Fetching all groups and members")
        groups_info = await asyncio.to_thread(self.security_info.fetch_groups)

        groups_members = {}
        for group_name, _ in groups_info.items():
            groups_members[group_name] = await asyncio.to_thread(
                self.security_info.fetch_members, group_name
            )

        self._logger.info("Fetching all users")
        users_info = await asyncio.to_thread(self.security_info.fetch_users)

        for user, sid in users_info.items():
            yield await self._user_access_control_doc(
                user, sid, groups_info, groups_members
            )

    async def get_entity_permission(self, file_path, file_type):
        if not self._dls_enabled():
            return []

        permissions = []
        if file_type == "file":
            list_permissions = await asyncio.to_thread(
                self.list_file_permission,
                file_path=file_path,
                file_type="file",
                mode="rb",
                access=FilePipePrinterAccessMask.READ_CONTROL,
            )
        else:
            list_permissions = await asyncio.to_thread(
                self.list_file_permission,
                file_path=file_path,
                file_type="dir",
                mode="br",
                access=DirectoryAccessMask.READ_CONTROL,
            )
        for permission in list_permissions:
            if (
                permission["ace_type"].value == ACCESS_ALLOWED_TYPE
                or permission["mask"].value == ACCESS_MASK_DENIED_WRITE_PERMISSION
            ):
                permissions.append(_prefix_sid(permission["sid"]))

        return permissions

    async def get_docs(self, filtering=None):
        """Executes the logic to fetch files and folders in async manner.
        Yields:
            dictionary: Dictionary containing the Network Drive files and folders as documents
        """

        if filtering and filtering.has_advanced_rules():
            advanced_rules = filtering.get_advanced_rules()
            matched_paths, invalid_rules = self.find_matching_paths(advanced_rules)
            if len(invalid_rules) > 0:
                raise InvalidRulesError(
                    f"Following advanced rules are invalid: {invalid_rules}"
                )

            for path in matched_paths:
                async for file in self.get_files(path=path):
                    if file["type"] == "folder":
                        yield file, None
                    else:
                        yield file, partial(self.get_content, file)

        else:
            matched_paths = (path for path, _, _ in self.get_directory_details)

            for path in matched_paths:
                async for file in self.get_files(path=path):
                    if file["type"] == "folder":
                        yield await self._decorate_with_access_control(
                            file, file.get("path"), file.get("type")
                        ), None
                    else:
                        yield await self._decorate_with_access_control(
                            file, file.get("path"), file.get("type")
                        ), partial(self.get_content, file)
