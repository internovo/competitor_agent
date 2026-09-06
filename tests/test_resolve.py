from app.logic import resolve
from app.models.schema import Candidate


def c(name, builder=None, rera=None, lat=None, lng=None, source="tavily"):
    return Candidate(name=name, builder=builder, rera_no=rera, lat=lat, lng=lng, source=source)


def test_same_rera_is_same():
    assert resolve.decide(c("Runwal Vertex", rera="P51800048221"), c("Vertex Phase 2", rera="P51800048221")) is True


def test_different_rera_is_different():
    assert resolve.decide(c("Runwal Vertex", rera="P51800048221"), c("Runwal Vertex", rera="P51800048222")) is False


def test_builder_name_inside_project_name_is_noise():
    assert resolve.decide(c("Runwal Vertex", "Runwal Group"), c("Vertex by Runwal")) is True


def test_same_builder_different_project():
    assert resolve.decide(c("Runwal Vertex", "Runwal Group"), c("Runwal Elegante", "Runwal Group")) is False


def test_different_builders_are_different():
    assert resolve.decide(c("Sky Heights", "Lodha"), c("Sky Heights", "Oberoi Realty")) is False


def test_nearby_with_shared_token_is_same():
    assert resolve.decide(c("Kalpataru Aurum Tower A", lat=19.19, lng=72.84), c("Aurum", lat=19.1905, lng=72.84)) is True


def test_partial_overlap_far_apart_is_ambiguous():
    assert resolve.decide(c("Chandak Highscape City"), c("Highscape Heights")) is None


def test_no_shared_tokens_is_different():
    assert resolve.decide(c("Sheth Nova"), c("Ajmera Skyline")) is False
