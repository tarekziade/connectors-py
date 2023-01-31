#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""
A task periodically clean up orphaned and stuck jobs.
"""

from connectors.byoc import ConnectorIndex, SyncJobIndex
from connectors.logger import logger
from connectors.services.base import BaseService

STUCK_JOB_ERROR = "The job has not seen any update for some time."


class JobCleanUpService(BaseService):
    def __init__(self, config):
        super().__init__(config)
        self.idling = int(self.service_config.get("job_cleanup_interval", 60 * 5))
        self.native_service_types = self.config.get("native_service_types", [])
        if "connector_id" in self.config:
            self.connectors_ids = [self.config.get("connector_id")]
        else:
            self.connectors_ids = []
        self.connector_index = None
        self.sync_job_index = None

    async def _run(self):
        logger.info("Successfully started Job cleanup task...")
        self.connector_index = ConnectorIndex(self.es_config)
        self.sync_job_index = SyncJobIndex(self.es_config)

        try:
            while self.running:
                await self._process_orphaned_jobs()
                await self._process_stuck_jobs()
                await self._sleeps.sleep(self.idling)
        finally:
            if self.connector_index is not None:
                self.connector_index.stop_waiting()
                await self.connector_index.close()
            if self.sync_job_index is not None:
                self.sync_job_index.stop_waiting()
                await self.sync_job_index.close()
        return 0

    async def _process_orphaned_jobs(self):
        try:
            logger.debug("Start cleaning up orphaned jobs...")
            connector_ids = [
                connector.id
                async for connector in self.connector_index.all_connectors()
            ]

            content_indices = set()
            job_ids = []
            async for job in self.sync_job_index.orphaned_jobs(
                connector_ids=connector_ids
            ):
                if job.index_name is not None:
                    content_indices.add(job.index_name)
                job_ids.append(job.job_id)

            if len(job_ids) == 0:
                logger.debug("No orphaned jobs found. Skipping...")
                return

            # delete content indices in case they are re-created by sync job
            if len(content_indices) > 0:
                await self.sync_job_index.delete_indices(indices=list(content_indices))
            result = await self.sync_job_index.delete_jobs(job_ids=job_ids)
            if len(result["failures"]) > 0:
                logger.error(f"Error found when deleting jobs: {result['failures']}")
            logger.info(
                f"Successfully deleted {result['deleted']} out of {result['total']} orphaned jobs."
            )
        except Exception as e:
            logger.critical(e, exc_info=True)
            self.raise_if_spurious(e)

    async def _process_stuck_jobs(self):
        try:
            logger.debug("Start cleaning up stuck jobs...")
            connector_ids = [
                connector.id
                async for connector in self.connector_index.supported_connectors(
                    native_service_types=self.native_service_types,
                    connectors_ids=self.connectors_ids,
                )
            ]

            marked_count = total_count = 0
            async for job in self.sync_job_index.stuck_jobs(
                connector_ids=connector_ids
            ):
                try:
                    connector_id = job.connector_id
                    connector = await self.connector_index.fetch_by_id(
                        doc_id=connector_id
                    )
                    if connector is None:
                        logger.warning(
                            f"Could not found connector by id #{connector_id}"
                        )
                    else:
                        await connector._sync_done(
                            job=job, result={}, exception=STUCK_JOB_ERROR
                        )
                        marked_count += 1
                except Exception as e:
                    logger.error(
                        f"Failed to mark stuck job #{job.job_id} as error: {e}"
                    )
                finally:
                    total_count += 1

            logger.info(
                f"Successfully marked #{marked_count} out of #{total_count} stuck jobs as error."
            )
        except Exception as e:
            logger.critical(e, exc_info=True)
            self.raise_if_spurious(e)
