import re
from pathlib import Path

import pytest
import yaml

from build_api_docs import DocsError, render_service

SPECS = Path(__file__).resolve().parent.parent / "specs"
SERVICES = sorted(p.name for p in SPECS.iterdir() if (p / "index.yaml").is_file())


def operation_ids(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "operationId":
                yield value
            else:
                yield from operation_ids(value)
    elif isinstance(node, list):
        for item in node:
            yield from operation_ids(item)


@pytest.mark.parametrize("service", SERVICES)
def test_every_operation_has_exactly_one_section(service):
    expected = sorted(
        op_id
        for path in (SPECS / service).rglob("*.yaml")
        for op_id in operation_ids(yaml.safe_load(path.read_text()))
    )

    rendered = sorted(re.findall(r'data-operation-id="([^"]+)"', render_service(SPECS / service / "index.yaml")))

    assert rendered == expected


def write_spec(tmp_path, operation):
    spec = {
        "openapi": "3.2.0",
        "info": {"title": "Test", "version": "1"},
        "paths": {"/things": {"post": {"operationId": "things.create", "summary": "Create Thing", **operation}}},
    }
    path = tmp_path / "index.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


OBJECT = {"type": "object", "properties": {"name": {"type": "string"}}}


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            {
                "requestBody": {"content": {"application/json": {"schema": OBJECT}}},
                "responses": {"204": {"description": "Done."}},
            },
            "POST /things request body has no example",
        ),
        (
            {"responses": {"200": {"description": "Done.", "content": {"application/json": {"schema": OBJECT}}}}},
            "POST /things 200 response body has no example",
        ),
    ],
    ids=["request", "response"],
)
def test_body_without_example_fails_the_build(tmp_path, operation, message):
    with pytest.raises(DocsError, match=message):
        render_service(write_spec(tmp_path, operation))
