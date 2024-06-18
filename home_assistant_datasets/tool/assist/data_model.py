"""Data model used for this evaluation.

This file defines dataclasses that hold data for the eval run, essentially the
output of parsing the yaml files.
"""

from typing import Any
from dataclasses import dataclass, field
from collections.abc import AsyncGenerator
import pathlib
from slugify import slugify

from mashumaro import DataClassDictMixin
from mashumaro.codecs.yaml import yaml_decode
import yaml


@dataclass
class EntityState(DataClassDictMixin):
    """An entity state or attributes"""

    state: str | None = None
    attributes: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Flattent to a dictionary."""
        data = {}
        if self.state is not None:
            data["state"] = self.state
        if self.attributes:
            data.update(self.attributes)
        return data


@dataclass
class Action(DataClassDictMixin):
    """An individual data item action."""

    sentences: list[str]
    """Sentences spoken."""

    setup: dict[str, EntityState]
    """Initial entity states to override."""

    expect_changes: dict[str, EntityState]
    """The device states to assert on."""

    ignore_changes: dict[str, list[str]] | None = None
    """The device state changes to ignored."""


@dataclass
class Record:
    """Represents an item in the dataset used to configure evaluation."""

    tests: list[Action] | None = field(default_factory=list)


@dataclass
class EvalTask:
    """Flattened detail about the task that is being evaluated."""

    output_dir: pathlib.Path
    """The eval task recordwriter output file."""

    synthetic_home_yaml: str
    """The synthetic home content to load."""

    record_id: str
    """Identifier for the synethetic home task."""

    input_text: str
    """The conversation input text to state."""

    expect_changes: dict[str, EntityState]
    """The device states to assert on."""

    ignore_changes: dict[str, list[str]] | None = None
    """The device state changes to ignored."""

    @property
    def task_id(self) -> str:
        """An identifier that labels this area summary evaluation task."""
        return f"{self.record_id}-{make_slug(self.input_text)}"


def make_slug(text: str) -> str:
    """Shorthand slugify command"""
    return slugify(text, separator="_")


def read_record(filename: pathlib.Path) -> Record:
    """Read the dataset record"""
    content = filename.read_text()
    return yaml_decode(content, Record)


def generate_tasks(
    record_path: pathlib.Path, dataset_path: pathlib.Path, output_dir: pathlib.Path
) -> AsyncGenerator[EvalTask, None]:
    """Read and validate the dataset."""
    # Generate the record id based on the file path
    relpath = record_path.relative_to(dataset_path)
    assert relpath.name.endswith(".yaml")
    record_id = make_slug(str(relpath)[:-5])

    # Find the fixtures for this directory. States will be overridden
    # below.
    fixture_path = record_path.parent / "_fixtures.yaml"
    synthetic_home_config = fixture_path.read_text()
    synthetic_home_yaml = yaml.load(synthetic_home_config, Loader=yaml.Loader)

    # Generate the set of eval tasks based on the sentences under test
    record = read_record(record_path)
    for action in record.tests:
        if not action.sentences:
            raise ValueError("No sentences defined for the action")

        # Override any state data
        entities = synthetic_home_yaml.get("entities", [])
        for entity_id, entity_state in action.setup.items():
            found_entity = next(
                iter(e for e in entities if e.get("id") == entity_id), None
            )
            if found_entity is None:
                raise ValueError(
                    f"No entity id '{entity_id}' found in fixture {fixture_path}"
                )
            if entity_state.state is not None:
                found_entity["state"] = entity_state.state
            if entity_state.attributes is not None:
                found_entity["attributes"] = {
                    **(found_entity.get("attributes", {})),
                    **entity_state.attributes,
                }
        yaml_contents = yaml.dump(
            synthetic_home_yaml, explicit_start=True, sort_keys=False
        )

        for sentence in action.sentences:
            yield EvalTask(
                output_dir=output_dir,
                synthetic_home_yaml=yaml_contents,
                record_id=record_id,
                input_text=sentence,
                expect_changes=action.expect_changes,
                ignore_changes=action.ignore_changes,
            )
