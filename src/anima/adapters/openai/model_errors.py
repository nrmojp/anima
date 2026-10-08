"""Provider errors translated without exposing credential-bearing messages."""
from functools import wraps
from openai import APIConnectionError, APITimeoutError, APIStatusError
from anima.core.model_contracts import ModelBackendError
from anima.core.external_errors import TransientExternalError
from anima.core.telemetry import emit


def model_errors(method):
    @wraps(method)
    async def call(self, request):
        try:
            return await method(self, request)
        except APITimeoutError as error:
            emit("decision.failed", operation=request.purpose, error_type="timeout")
            raise TransientExternalError("decision timeout", reason="timeout") from error
        except APIConnectionError as error:
            emit("decision.failed", operation=request.purpose, error_type="connection_error")
            raise TransientExternalError("decision connection failed", reason="connection_error") from error
        except APIStatusError as error:
            reason = "rate_limit" if error.status_code == 429 else "authentication" if error.status_code in {401,403} else "provider_failure"
            emit("decision.failed", operation=request.purpose, error_type=reason)
            raise ModelBackendError(reason) from error
        except (ValueError, KeyError, TypeError) as error:
            emit("decision.failed", operation=request.purpose, error_type="invalid_response")
            raise ModelBackendError("invalid_response") from error
    return call
