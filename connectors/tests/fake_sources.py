#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""
Collection of fake source classes for tests
"""
from functools import partial
from unittest.mock import Mock

from connectors.filtering.validation import (
    FilteringValidationResult,
    FilteringValidationState,
)


class FakeSource:
    """Fakey"""

    service_type = "fake"

    def __init__(self, connector):
        self.connector = connector
        if connector.configuration.has_field("raise"):
            raise Exception("I break on init")
        self.fail = connector.configuration.has_field("fail")

    async def changed(self):
        return True

    async def ping(self):
        pass

    async def close(self):
        pass

    async def _dl(self, doc_id, timestamp=None, doit=None):
        if not doit:
            return
        return {"_id": doc_id, "_timestamp": timestamp, "text": "xx"}

    async def get_docs(self):
        if self.fail:
            raise Exception("I fail while syncing")
        yield {"_id": "1"}, partial(self._dl, "1")

    @classmethod
    def get_default_configuration(cls):
        return []

    @classmethod
    async def validate_filtering(cls, filtering):
        # being explicit about that this result should always be valid
        return FilteringValidationResult(
            state=FilteringValidationState.VALID, errors=[]
        )

    def tweak_bulk_options(self, options):
        pass


class FakeSourceFilteringValid(FakeSource):
    """Source with valid filtering."""

    service_type = "filtering_state_valid"

    @classmethod
    async def validate_filtering(cls, filtering):
        # use separate fake source to not rely on the behaviour in FakeSource which is used in many tests
        return FilteringValidationResult(
            state=FilteringValidationState.VALID, errors=[]
        )


class FakeSourceFilteringStateInvalid(FakeSource):
    """Source with filtering in state invalid."""

    service_type = "filtering_state_invalid"

    @classmethod
    async def validate_filtering(cls, filtering):
        return FilteringValidationResult(state=FilteringValidationState.INVALID)


class FakeSourceFilteringStateEdited(FakeSource):
    """Source with filtering in state edited."""

    service_type = "filtering_state_edited"

    @classmethod
    async def validate_filtering(cls, filtering):
        return FilteringValidationResult(state=FilteringValidationState.EDITED)


class FakeSourceFilteringErrorsPresent(FakeSource):
    """Source with filtering errors."""

    service_type = "filtering_errors_present"

    @classmethod
    async def validate_filtering(cls, filtering):
        return FilteringValidationResult(errors=[Mock()])


class FakeSourceTS(FakeSource):
    """Fake source with stable TS"""

    service_type = "fake_ts"
    ts = "2022-10-31T09:04:35.277558"

    async def get_docs(self):
        if self.fail:
            raise Exception("I fail while syncing")
        yield {"_id": "1", "_timestamp": self.ts}, partial(self._dl, "1")


class FailsThenWork(FakeSource):
    """Buggy"""

    service_type = "fail_once"
    fail = True

    async def get_docs(self):
        if FailsThenWork.fail:
            FailsThenWork.fail = False
            raise Exception("I fail while syncing")
        yield {"_id": "1"}, partial(self._dl, "1")


class LargeFakeSource(FakeSource):
    """Phatey"""

    service_type = "large_fake"

    async def get_docs(self):
        for i in range(1001):
            doc_id = str(i + 1)
            yield {"_id": doc_id, "data": "big" * 1024 * 1024}, partial(
                self._dl, doc_id
            )
