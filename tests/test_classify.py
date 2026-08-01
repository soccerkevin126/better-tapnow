from pathlib import Path

from tapnow.classify import classify_folder, validate_inputs
from tapnow.config import CountSpec, InputsConfig, RoleSpec


def make_flat_spec(**expects):
    return InputsConfig(mode="flat", expects={k: CountSpec(**v) for k, v in expects.items()})


def test_flat_classification(tmp_path: Path):
    (tmp_path / "brief.md").write_text("hi")
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")

    spec = make_flat_spec(text={"min": 1, "max": 1}, image={"min": 2})
    inputs = classify_folder(tmp_path, spec)

    assert [f.name for f in inputs.by_type["text"]] == ["brief.md"]
    assert len(inputs.by_type["image"]) == 2
    assert validate_inputs(inputs, spec) == []


def test_flat_declared_types_seed_empty_lists(tmp_path: Path):
    # `inputs.image` must resolve to [] (not error) when the optional type
    # has no files — workflows reference it unconditionally.
    (tmp_path / "brief.md").write_text("x")
    spec = make_flat_spec(text={"min": 1}, image={"min": 0})
    inputs = classify_folder(tmp_path, spec)
    assert inputs.by_type["image"] == []
    assert validate_inputs(inputs, spec) == []


def test_flat_missing_and_excess(tmp_path: Path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "b.md").write_text("x")
    spec = make_flat_spec(text={"min": 1, "max": 1}, image={"min": 1})
    problems = validate_inputs(classify_folder(tmp_path, spec), spec)
    assert any("at most 1 text" in p for p in problems)
    assert any("at least 1 image" in p for p in problems)


def test_unclassifiable_file_is_reported(tmp_path: Path):
    (tmp_path / "brief.md").write_text("x")
    (tmp_path / "mystery.xyz123").write_bytes(b"x")
    spec = make_flat_spec(text={"min": 1})
    problems = validate_inputs(classify_folder(tmp_path, spec), spec)
    assert any("mystery.xyz123" in p for p in problems)


def test_roles_mode(tmp_path: Path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "style.png").write_bytes(b"x")
    (tmp_path / "content").mkdir()
    (tmp_path / "content" / "a.jpg").write_bytes(b"x")

    spec = InputsConfig(mode="roles", roles={
        "reference": RoleSpec(type="image", min=1, max=1),
        "content": RoleSpec(type="image", min=1),
    })
    inputs = classify_folder(tmp_path, spec)
    assert validate_inputs(inputs, spec) == []
    assert inputs.by_role["reference"][0].name == "style.png"


def test_roles_missing_subfolder_and_wrong_type(tmp_path: Path):
    (tmp_path / "content").mkdir()
    (tmp_path / "content" / "notes.md").write_text("x")
    spec = InputsConfig(mode="roles", roles={
        "reference": RoleSpec(type="image", min=1),
        "content": RoleSpec(type="image", min=1),
    })
    problems = validate_inputs(classify_folder(tmp_path, spec), spec)
    assert any("missing role subfolder 'reference/'" in p for p in problems)
    assert any("role 'content' expects image" in p for p in problems)
