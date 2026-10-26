"""Demo image bytes use the same upload and promotion contracts as real data."""

from io import BytesIO

import pytest
from PIL import Image

from src.core.demo_data.documents import _catalogue_image

pytestmark = pytest.mark.unit


def test_demo_catalogue_image_is_a_small_deterministic_valid_png() -> None:
    payload = _catalogue_image()
    assert payload == _catalogue_image()
    assert len(payload) < 50_000
    with Image.open(BytesIO(payload)) as image:
        assert image.format == "PNG"
        assert image.size == (600, 360)
        image.verify()
