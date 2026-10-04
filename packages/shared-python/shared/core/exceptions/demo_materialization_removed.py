"""Migration response for the retired personal demo copy operation."""

from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.core.response.ErrorCode import ErrorCode


class DemoMaterializationRemovedException(KnowhereException):
    def __init__(self) -> None:
        super().__init__(code=ErrorCode.DEMO_MATERIALIZATION_REMOVED, internal_message="Personal demo materialization has been retired.", user_message="Use catalog canonical_document_id values to query namespace __knowhere_demo__. Existing personal copies remain accessible through the document API.", details={"namespace": "__knowhere_demo__"}, http_status_code=410)
