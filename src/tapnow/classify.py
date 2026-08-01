"""Classify an input folder by media type and validate it against a workflow's
declared inputs — before any money is spent."""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from .config import InputsConfig, MediaType

# Extensions mimetypes gets wrong or misses for our purposes.
_EXTRA: dict[str, MediaType] = {
    ".md": "text", ".txt": "text", ".yaml": "text", ".yml": "text", ".json": "text",
    ".webp": "image", ".heic": "image",
    ".m4a": "audio",
    ".webm": "video", ".mov": "video",
}


@dataclass
class InputFile:
    path: Path
    type: MediaType
    role: str | None = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def content(self) -> str:
        if self.type != "text":
            raise ValueError(f"{self.path} is {self.type}, not text")
        return self.path.read_text()


@dataclass
class ClassifiedInputs:
    by_type: dict[str, list[InputFile]] = field(default_factory=dict)
    by_role: dict[str, list[InputFile]] = field(default_factory=dict)
    unclassified: list[Path] = field(default_factory=list)
    missing_roles: list[str] = field(default_factory=list)


def classify_file(path: Path) -> MediaType | None:
    if path.suffix.lower() in _EXTRA:
        return _EXTRA[path.suffix.lower()]
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        prefix = mime.split("/")[0]
        if prefix in ("text", "image", "audio", "video"):
            return prefix  # type: ignore[return-value]
    return None


def _classify_dir(d: Path, role: str | None = None) -> tuple[list[InputFile], list[Path]]:
    files, unknown = [], []
    for p in sorted(d.iterdir()):
        if p.name.startswith(".") or p.is_dir():
            continue
        t = classify_file(p)
        if t is None:
            unknown.append(p)
        else:
            files.append(InputFile(path=p, type=t, role=role))
    return files, unknown


def classify_folder(folder: Path, spec: InputsConfig) -> ClassifiedInputs:
    out = ClassifiedInputs()
    if spec.mode == "flat":
        # Seed declared types so refs like `inputs.image` resolve to [] when
        # an optional type has no files, instead of erroring.
        for mtype in spec.expects:
            out.by_type.setdefault(mtype, [])
        files, unknown = _classify_dir(folder)
        out.unclassified = unknown
        for f in files:
            out.by_type.setdefault(f.type, []).append(f)
    else:
        for role in spec.roles:
            out.by_role.setdefault(role, [])
            sub = folder / role
            if not sub.is_dir():
                out.missing_roles.append(role)
                continue
            files, unknown = _classify_dir(sub, role=role)
            out.unclassified.extend(unknown)
            out.by_role[role] = files
            for f in files:
                out.by_type.setdefault(f.type, []).append(f)
    return out


def validate_inputs(inputs: ClassifiedInputs, spec: InputsConfig) -> list[str]:
    """Returns a list of human-readable problems; empty means valid."""
    problems: list[str] = []
    if spec.mode == "flat":
        for mtype, count in spec.expects.items():
            n = len(inputs.by_type.get(mtype, []))
            if n < count.min:
                problems.append(f"need at least {count.min} {mtype} file(s), found {n}")
            if count.max is not None and n > count.max:
                problems.append(f"at most {count.max} {mtype} file(s) allowed, found {n}")
    else:
        for role, rspec in spec.roles.items():
            if role in inputs.missing_roles:
                if rspec.min > 0:
                    problems.append(f"missing role subfolder '{role}/' ({rspec.type}, min {rspec.min})")
                continue
            files = inputs.by_role.get(role, [])
            wrong = [f.name for f in files if f.type != rspec.type]
            if wrong:
                problems.append(f"role '{role}' expects {rspec.type}, but found: {', '.join(wrong)}")
            n = len([f for f in files if f.type == rspec.type])
            if n < rspec.min:
                problems.append(f"role '{role}' needs at least {rspec.min} {rspec.type} file(s), found {n}")
            if rspec.max is not None and n > rspec.max:
                problems.append(f"role '{role}' allows at most {rspec.max} file(s), found {n}")
    for p in inputs.unclassified:
        problems.append(f"cannot classify '{p.name}' by type — remove it or rename with a known extension")
    return problems
