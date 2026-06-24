"""Tests for the markdown parser used by spec/plan/strategy/research projections."""

from qtea.md_parser import (
    extract_bullets,
    extract_tables,
    parse_markdown,
    section_to_dict,
    slugify,
)

SAMPLE = """\
# Login Feature

Some intro prose.

## Acceptance Criteria

- User can sign in with valid credentials
- Invalid creds show error
1. Numbered also works

## User Flow

```pseudo
goto /login
fill creds
```

### Sub heading

content under sub

## Test Data

| name | value | role  |
| ---- | ----- | ----- |
| a    | 1     | admin |
| b    | 2     | user  |
"""


def test_parse_builds_tree_with_correct_levels():
    root = parse_markdown(SAMPLE)
    assert root.level == 0
    assert len(root.children) == 1
    feature = root.children[0]
    assert feature.title == "Login Feature"
    assert feature.level == 1
    titles = [c.title for c in feature.children]
    assert titles == ["Acceptance Criteria", "User Flow", "Test Data"]
    user_flow = feature.children[1]
    assert any(c.title == "Sub heading" for c in user_flow.children)


def test_find_is_case_insensitive_substring():
    root = parse_markdown(SAMPLE)
    sec = root.find("acceptance")
    assert sec is not None and sec.title == "Acceptance Criteria"
    assert root.find("does-not-exist") is None


def test_extract_bullets_handles_dash_and_numbered():
    root = parse_markdown(SAMPLE)
    ac = root.find("acceptance criteria")
    bullets = extract_bullets(ac.content)
    assert "User can sign in with valid credentials" in bullets
    assert "Invalid creds show error" in bullets
    assert "Numbered also works" in bullets


def test_code_fence_not_treated_as_heading():
    root = parse_markdown(SAMPLE)
    flow = root.find("user flow")
    # Sub heading is a real heading inside section
    assert any(c.title == "Sub heading" for c in flow.children)
    # Fence content preserved in flow.content
    assert "goto /login" in flow.content


def test_extract_tables_parses_header_and_rows():
    root = parse_markdown(SAMPLE)
    td = root.find("test data")
    tables = extract_tables(td.content)
    assert len(tables) == 1
    header, *rows = tables[0]
    assert header == ["name", "value", "role"]
    assert rows[0] == ["a", "1", "admin"]
    assert rows[1] == ["b", "2", "user"]


def test_section_to_dict_recursive_shape():
    root = parse_markdown(SAMPLE)
    d = section_to_dict(root.children[0])
    assert d["title"] == "Login Feature"
    assert d["level"] == 1
    assert isinstance(d["bullets"], list)
    assert isinstance(d["tables"], list)
    child_titles = [c["title"] for c in d["children"]]
    assert child_titles == ["Acceptance Criteria", "User Flow", "Test Data"]


def test_slugify_strips_and_lowers():
    assert slugify("Login Feature!") == "login-feature"
    assert slugify("  Multiple   Spaces ") == "multiple-spaces"
    assert slugify("???", prefix="REQ-") == "REQ-untitled"
