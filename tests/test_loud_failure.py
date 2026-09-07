"""A broken run must not wear the costume of an empty one.

If the key is wrong or we are throttled, extraction fails on every page and the
scan otherwise succeeds with every field unknown. To a sales rep that reads as
"there is no data on these competitors", and nobody would ever know why.
"""
import asyncio

import pytest

from app.llm.client import (
    ExtractionDegraded, FatalLLMError, LLMAuthError, LLMRateLimitError, fatal_for,
)


class Boom(Exception):
    def __init__(self, status=None, name=None):
        super().__init__(f"boom {status}")
        if status is not None:
            self.status_code = status


def _named(name, **kw):
    return type(name, (Boom,), {})(**kw)


# --- which failures are fatal ----------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_error_is_fatal(status):
    assert isinstance(fatal_for(Boom(status)), LLMAuthError)


def test_a_rate_limit_after_retries_is_fatal():
    assert isinstance(fatal_for(Boom(429)), LLMRateLimitError)


def test_provider_exception_names_are_recognised_without_importing_them():
    assert isinstance(fatal_for(_named("AuthenticationError")), LLMAuthError)
    assert isinstance(fatal_for(_named("PermissionDeniedError")), LLMAuthError)
    assert isinstance(fatal_for(_named("RateLimitError")), LLMRateLimitError)


@pytest.mark.parametrize("e", [Boom(500), Boom(400), ValueError("bad json"), TimeoutError()])
def test_everything_else_is_a_page_failure_not_a_run_failure(e):
    """One page that would not parse is a thin project. It is not a broken run."""
    assert fatal_for(e) is None


def test_the_error_types_are_the_ones_the_api_reports():
    assert LLMAuthError.type == "llm_auth"
    assert LLMRateLimitError.type == "llm_rate_limited"
    assert ExtractionDegraded.type == "extraction_degraded"
    assert issubclass(LLMAuthError, FatalLLMError)


# --- the counters and the gate ----------------------------------------------

class FakeLLM:
    def __init__(self, attempted, failed):
        self.calls_attempted, self.calls_failed = attempted, failed

    @property
    def failure_rate(self):
        return self.calls_failed / self.calls_attempted if self.calls_attempted else 0.0


def _narrate(llm, own, projects):
    from app.graph.nodes import pipeline

    state = {"own": own, "radius_km": 1.5, "projects": projects, "log": []}
    return asyncio.run(pipeline.narrate(state, {"configurable": {"llm": llm}}))


def test_a_run_where_most_calls_failed_ends_failed(own, full_project):
    with pytest.raises(ExtractionDegraded) as e:
        _narrate(FakeLLM(10, 6), own, [full_project])
    assert e.value.type == "extraction_degraded"
    assert e.value.stage == "narrate"
    assert "6 of 10" in str(e.value)


def test_a_run_just_over_the_threshold_ends_failed(own, full_project):
    with pytest.raises(ExtractionDegraded):
        _narrate(FakeLLM(10, 3), own, [full_project])


def test_a_run_at_or_under_the_threshold_survives(own, full_project):
    """One page in four that would not parse is a coverage problem, reported as
    absences. It is not a reason to throw the run away."""
    out = _narrate(FakeLLM(12, 3), own, [full_project])
    assert out["projects"][0].insight


def test_a_clean_run_is_unaffected(own, full_project):
    out = _narrate(FakeLLM(14, 0), own, [full_project])
    assert out["projects"][0].insight_source == "template"


def test_no_llm_configured_is_not_a_failure(own, full_project):
    out = _narrate(None, own, [full_project])
    assert out["projects"][0].insight


# --- the counters actually count --------------------------------------------

def test_every_model_call_is_counted_and_a_fatal_one_stops_the_run():
    from app.llm.client import LLM

    from app.cost import UsageCounter

    class Chat:
        async def ainvoke(self, messages, config=None):
            raise Boom(401)

    llm = LLM.__new__(LLM)
    llm.calls_attempted = llm.calls_failed = 0
    llm.usage = UsageCounter()
    with pytest.raises(LLMAuthError):
        asyncio.run(llm._call(Chat(), []))
    assert (llm.calls_attempted, llm.calls_failed) == (1, 1)


def test_a_page_failure_is_counted_and_re_raised_for_the_caller_to_absorb():
    from app.llm.client import LLM

    from app.cost import UsageCounter

    class Chat:
        async def ainvoke(self, messages, config=None):
            raise ValueError("could not parse")

    llm = LLM.__new__(LLM)
    llm.calls_attempted = llm.calls_failed = 0
    llm.usage = UsageCounter()
    with pytest.raises(ValueError):
        asyncio.run(llm._call(Chat(), []))
    assert llm.failure_rate == 1.0


# --- a page that truly says nothing is still an absence, not a failure ------

def test_a_page_that_states_nothing_yields_absence_reasons_not_a_failed_run(own):
    from tests.test_deterministic_first import CountingLLM, run_extract

    project, _ = run_extract("Register your interest today. Site visit available.", own,
                             CountingLLM(), name="Godrej Prime Malad")
    for field in ("configurations", "rate_psf", "possession"):
        assert project.report(field).absent
        assert project.report(field).label
