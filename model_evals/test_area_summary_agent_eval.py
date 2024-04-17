"""An evaluation for the Summary Agent custom component summarizing an area with pruned context."""

from collections.abc import Generator, Callable, Awaitable
import logging
import pathlib
from typing import Any
import uuid
from slugify import slugify
import dataclasses

import pytest
from pytest_subtests import SubTests
import yaml

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import area_registry as ar, entity_registry as er, device_registry as dr

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.synthetic_home.home_model.device_types import load_restorable_attributes

from .conftest import ConversationAgent, EvalRecordWriter
from .common import SyntheticDeviceState, HomeAssistantContext, ModelConfig, AreaSummaryTask


_LOGGER = logging.getLogger(__name__)

MODEL_EVAL_OUTPUT = "model_outputs/area_summary_agent"

STRIP_PREFIX = "Summary: "

@pytest.fixture(
    name="model_config",
    params=[
        (ModelConfig("google_generative_ai_conversation", "gemini-pro")),
        (ModelConfig("openai_conversation", "gpt-3.5")),
        (ModelConfig("vicuna_conversation", "mistral-7b-instruct")),
    ],
)
def model_config_fixture(request: pytest.FixtureRequest) -> ModelConfig:
    """Fiture that defines which model is being evaluated."""
    return request.param


@pytest.fixture(name="base_conversation_agent_id")
async def mock_base_conversation_agent_id(
    model_config: ModelConfig,
    openai_config_entry: MockConfigEntry,
    vicuna_conversation_config_entry: MockConfigEntry,
    google_genai_config_entry: MockConfigEntry,
) -> str:
    """Return the id for the conversation agent under test."""
    if model_config.conversation_agent_domain == "openai_conversation":
        return openai_config_entry.entry_id
    if model_config.conversation_agent_domain == "google_generative_ai_conversation":
        return google_genai_config_entry.entry_id
    if model_config.conversation_agent_domain == "vicuna_conversation":
        return vicuna_conversation_config_entry.entry_id
    raise ValueError(f"Conversation Agent domain not found: {model_config.conversation_agent_domain}")


@pytest.fixture(name="summary_agent_config_entry")
async def mock_summary_agent(hass: HomeAssistant, base_conversation_agent_id: str) -> MockConfigEntry:
    # Ensure custom components used in the test are loaded
    from custom_components import summary_agent  # noqa: F401

    config_entry = MockConfigEntry(
        domain="summary_agent",
        data={
            "agent_id": base_conversation_agent_id,
        },
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state == ConfigEntryState.LOADED
    return config_entry


@pytest.fixture(name="conversation_agent_id")
async def mock_conversation_agent_id(
    summary_agent_config_entry: MockConfigEntry,
) -> str:
    """Return the id for the conversation agent under test."""
    return summary_agent_config_entry.entry_id


def cleanup_response(response: str) -> str:
    """Perform any cleanup on the response where the LLM returns part of the prompt."""
    response = response.lstrip()
    try:
        index = response.index(STRIP_PREFIX)
    except ValueError:
        return response
    return response[index+len(STRIP_PREFIX):]


@pytest.fixture(name="eval_record_writer")
def eval_record_writer_fixture(
    hass: HomeAssistant,
    model_config: ModelConfig,
    synthetic_home_config: str
) -> Generator[EvalRecordWriter, None, None]:
    """Fixture that prepares the eval output writer."""
    writer = EvalRecordWriter(
        pathlib.Path(MODEL_EVAL_OUTPUT) / model_config.model_id,
        pathlib.Path(synthetic_home_config).name,
    )
    writer.open()
    yield writer
    writer.close()


@pytest.fixture(name="tasks_provider")
def tasks_provider_fixture(
    hass: HomeAssistant,
    synthetic_home_yaml: str
) -> Callable[[], Generator[AreaSummaryTask, None, None]]:
    """Fixture that generates the tasks to evaluate."""

    synthetic_home_config = yaml.load(synthetic_home_yaml, Loader=yaml.Loader)
    home_name = synthetic_home_config["name"]
    home_id = slugify(home_name)

    area_registry = ar.async_get(hass)
    area_entries = list(area_registry.async_list_areas())
    _LOGGER.info("Loaded %s areas to evaluate", len(area_entries))

    def func() -> Generator[AreaSummaryTask, None, None]:
        for area_entry in area_entries:
            area_name = area_entry.name

            if (devices := synthetic_home_config["devices"].get(area_name)) is None:
                return
            for device_info in devices:
                attributes = load_restorable_attributes(device_info["device_type"])
                for attribute in attributes:
                    device_state = SyntheticDeviceState(device_info["name"], attribute)

                    yield AreaSummaryTask(
                        home_id=home_id,
                        home_name=home_name,
                        area_id=area_entry.id,
                        area_name=area_name,
                        device_state=device_state,
                    )

    return func


def get_device_eval_context(hass: HomeAssistant, device_entry: dr.DeviceEntry) -> dict[str, Any]:
    """Return information about a device used for dumping a context record."""
    detail = {}
    # Dump device information
    if device_entry.model:
        detail["model"] = device_entry.model
    if device_entry.manufacturer:
        detail["manufacturer"] = device_entry.manufacturer
    # Dump entity information
    entity_registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_device(entity_registry, device_entry.id):
        if state := hass.states.get(entity_entry.entity_id):
            state_str = state.state
            if uom := state.attributes.get("unit_of_measurement"):
                state_str = f"{state_str} {uom}"
            detail[entity_entry.entity_id] = state_str
    return detail


@pytest.fixture(name="prepare_state")
async def prepare_state_fixture(
    hass: HomeAssistant,
    synthetic_home_config_entry: ConfigEntry
) -> Callable[[AreaSummaryTask], Awaitable[None]]:
    """Fixture with a function call to change device state for evaluation."""

    async def func(area_summary_task: AreaSummaryTask) -> None:
        device_state = area_summary_task.device_state
        _LOGGER.info("Changing device state for %s to %s", device_state.device_name, device_state.restorable_attribute)
        await hass.services.async_call(
            "synthetic_home",
            "set_synthetic_device_state",
            service_data={
                "config_entry_id": synthetic_home_config_entry.entry_id,
                "area": area_summary_task.area_name,
                "device": device_state.device_name,
                "restorable_attribute_key": device_state.restorable_attribute,
            },
            blocking=True,
        )

        device_registry = dr.async_get(hass)
        return HomeAssistantContext(device_context={
            device_entry.name: get_device_eval_context(hass, device_entry)
            for device_entry in dr.async_entries_for_area(device_registry, area_summary_task.area_id)
        })

    return func


@pytest.mark.parametrize(
    ("synthetic_home_config"),
    [
        ("datasets/devices/home1-us.yaml"),
        ("datasets/devices/apartament4-pl.yaml"),
        ("datasets/devices/casa-adosada-en-la-costa-es.yaml"),
        ("datasets/devices/lakeside-retreat-de.yaml"),
    ],
)
async def test_collect_area_summaries(
    hass: HomeAssistant,
    agent: ConversationAgent,
    eval_record_writer: EvalRecordWriter,
    subtests: SubTests,
    tasks_provider: Callable[[], Generator[AreaSummaryTask, None, None]],
    prepare_state: Callable[[AreaSummaryTask], Awaitable[HomeAssistantContext]],
) -> None:
    """Collects model responses for area summaries."""

    for i, area_summary_task in enumerate(tasks_provider()):
        with subtests.test(msg=area_summary_task.task_id, i=i):

            # Setup the home for evaluation
            context = await prepare_state(area_summary_task)

            # Run the conversation agent
            area_name = area_summary_task.area_name
            _LOGGER.info("Performing area summary: %s", area_name)
            response = await agent.async_process(hass, area_name)
            _LOGGER.debug("Area summary response: %s", response)
            response = cleanup_response(response)

            eval_record_writer.write(
                {
                    "uuid": str(uuid.uuid4()),  # Unique based on the model evaluated
                    "task_id": area_summary_task.task_id,
                    "task": dataclasses.asdict(area_summary_task),
                    "response": response,
                    "context": dataclasses.asdict(context),
                }
            )