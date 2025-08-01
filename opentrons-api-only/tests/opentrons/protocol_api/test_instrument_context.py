"""Tests for the InstrumentContext public interface."""

import inspect
import pytest
from collections import OrderedDict
from datetime import datetime
from typing import ContextManager, Optional, Any
from unittest.mock import sentinel

from decoy import Decoy, matchers
from pytest_lazy_fixtures import lf as lazy_fixture

from opentrons.protocol_engine.commands.pipetting_common import LiquidNotFoundError
from opentrons.protocol_engine.errors.error_occurrence import (
    ProtocolCommandFailedError,
)

from opentrons.legacy_broker import LegacyBroker
from opentrons.protocols.advanced_control.transfers.common import (
    TransferTipPolicyV2,
    TransferTipPolicyV2Type,
)
from opentrons.protocols.advanced_control.transfers import (
    transfer_liquid_utils as mock_tx_liquid_utils,
)

from tests.opentrons.protocol_api.partial_tip_configurations import (
    PipetteReliantNozzleConfigSpec,
    PIPETTE_RELIANT_TEST_SPECS,
    NozzleLayoutArgs,
    PipetteIndependentNozzleConfigSpec,
    PIPETTE_INDEPENDENT_TEST_SPECS,
    InstrumentCoreNozzleConfigSpec,
    INSTRUMENT_CORE_NOZZLE_LAYOUT_TEST_SPECS,
    ExpectedCoreArgs,
)
from opentrons.protocols.api_support import instrument as mock_instrument_support
from opentrons.protocols.api_support.types import APIVersion
from opentrons.protocols.api_support.util import (
    APIVersionError,
    UnsupportedAPIError,
    FlowRates,
    PlungerSpeeds,
)
from opentrons.protocol_api import (
    MAX_SUPPORTED_VERSION,
    InstrumentContext,
    Labware,
    Well,
    labware,
    LiquidClass,
)
from opentrons.protocol_api.core.common import InstrumentCore, ProtocolCore
from opentrons.protocol_api.core.legacy.legacy_instrument_core import (
    LegacyInstrumentCore,
)

from opentrons.hardware_control.nozzle_manager import NozzleMap
from opentrons.protocol_api.disposal_locations import TrashBin, WasteChute
from opentrons.types import (
    Location,
    Mount,
    Point,
    NozzleMapInterface,
    MeniscusTrackingTarget,
)

from opentrons_shared_data.pipette.pipette_definition import ValidNozzleMaps
from opentrons_shared_data.errors.exceptions import (
    CommandPreconditionViolated,
)
from opentrons_shared_data.liquid_classes.liquid_class_definition import (
    LiquidClassSchemaV1,
)
from opentrons_shared_data.robot.types import RobotType
from . import versions_at_or_above, versions_between


@pytest.fixture(autouse=True)
def _mock_instrument_support_module(
    decoy: Decoy, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, func in inspect.getmembers(mock_instrument_support, inspect.isfunction):
        monkeypatch.setattr(mock_instrument_support, name, decoy.mock(func=func))


@pytest.fixture(autouse=True)
def _mock_labware_module(decoy: Decoy, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, func in inspect.getmembers(labware, inspect.isfunction):
        monkeypatch.setattr(labware, name, decoy.mock(func=func))


@pytest.fixture(autouse=True)
def _mock_transfer_liquid_utils(decoy: Decoy, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, func in inspect.getmembers(mock_tx_liquid_utils, inspect.isfunction):
        monkeypatch.setattr(mock_tx_liquid_utils, name, decoy.mock(func=func))


@pytest.fixture
def mock_instrument_core(decoy: Decoy) -> InstrumentCore:
    """Get a mock instrument implementation core."""
    instrument_core = decoy.mock(cls=InstrumentCore)
    decoy.when(instrument_core.get_mount()).then_return(Mount.LEFT)
    decoy.when(instrument_core._pressure_supported_by_pipette()).then_return(True)
    # we need to add this for the mock of liquid_presence detection to actually work
    # this replaces the mock with a a property again
    instrument_core._liquid_presence_detection = False  # type: ignore[attr-defined]

    def _setter(enable: bool) -> None:
        instrument_core._liquid_presence_detection = enable  # type: ignore[attr-defined]

    def _getter() -> bool:
        return instrument_core._liquid_presence_detection  # type: ignore[attr-defined, no-any-return]

    instrument_core.get_liquid_presence_detection = _getter  # type: ignore[method-assign]
    instrument_core.set_liquid_presence_detection = _setter  # type: ignore[method-assign]

    return instrument_core


@pytest.fixture
def mock_legacy_instrument_core(decoy: Decoy) -> LegacyInstrumentCore:
    """Get a mock instrument implementation core."""
    instrument_core = decoy.mock(cls=LegacyInstrumentCore)
    decoy.when(instrument_core.get_mount()).then_return(Mount.LEFT)
    return instrument_core


@pytest.fixture
def mock_protocol_core(decoy: Decoy) -> ProtocolCore:
    """Get a mock ProtocolCore."""
    return decoy.mock(cls=ProtocolCore)


@pytest.fixture
def mock_broker(decoy: Decoy) -> LegacyBroker:
    """Get a mock command message broker."""
    return decoy.mock(cls=LegacyBroker)


@pytest.fixture
def mock_trash(decoy: Decoy) -> Labware:
    """Get a mock fixed-trash labware."""
    return decoy.mock(cls=Labware)


@pytest.fixture
def api_version() -> APIVersion:
    """Get the API version to test at."""
    return MAX_SUPPORTED_VERSION


@pytest.fixture
def subject(
    mock_instrument_core: InstrumentCore,
    mock_protocol_core: ProtocolCore,
    mock_broker: LegacyBroker,
    mock_trash: Labware,
    api_version: APIVersion,
) -> InstrumentContext:
    """Get a ProtocolCore test subject with its dependencies mocked out."""
    return InstrumentContext(
        core=mock_instrument_core,
        protocol_core=mock_protocol_core,
        broker=mock_broker,
        api_version=api_version,
        tip_racks=[],
        trash=mock_trash,
        requested_as="requested-pipette-name",
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 0), APIVersion(2, 1)])
def test_api_version(api_version: APIVersion, subject: InstrumentContext) -> None:
    """It should have an api_version property."""
    assert subject.api_version == api_version


@pytest.mark.parametrize("channels_from_core", [1, 8, 96])
def test_channels(
    decoy: Decoy,
    subject: InstrumentContext,
    mock_instrument_core: InstrumentCore,
    channels_from_core: int,
) -> None:
    """It should return the number of channels, as returned by the core."""
    decoy.when(mock_instrument_core.get_channels()).then_return(channels_from_core)
    assert subject.channels == channels_from_core


@pytest.mark.parametrize(
    ("channels_from_core", "expected_type"),
    [
        (1, "single"),
        (8, "multi"),
        (96, "multi"),
    ],
)
def test_type(
    decoy: Decoy,
    subject: InstrumentContext,
    mock_instrument_core: InstrumentCore,
    channels_from_core: int,
    expected_type: str,
) -> None:
    """It should map the number of channels from the core into the string "single" or "multi"."""
    decoy.when(mock_instrument_core.get_channels()).then_return(channels_from_core)
    assert subject.type == expected_type


def test_trash_container(
    decoy: Decoy,
    mock_trash: Labware,
    subject: InstrumentContext,
) -> None:
    """It should have a settable trash_container property."""
    assert subject.trash_container is mock_trash

    other_trash = decoy.mock(cls=Labware)
    subject.trash_container = other_trash

    assert subject.trash_container is other_trash


def test_tip_racks(decoy: Decoy, subject: InstrumentContext) -> None:
    """It should have a settable tip_racks property."""
    assert subject.tip_racks == []

    tip_racks = [decoy.mock(cls=Labware), decoy.mock(cls=Labware)]
    subject.tip_racks = tip_racks

    assert subject.tip_racks == tip_racks


def test_mount(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should have a mount property."""
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    assert subject.mount == "right"


def test_move_to(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should move the pipette to a location."""
    location = Location(point=Point(1, 2, 3), labware=None)

    subject.move_to(location)

    decoy.verify(
        mock_instrument_core.move_to(
            well_core=None,
            location=location,
            force_direct=False,
            minimum_z_height=None,
            speed=None,
            check_for_movement_conflicts=False,
        ),
        times=1,
    )


def test_move_to_well(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should move the pipette to a location."""
    mock_well = decoy.mock(cls=Well)
    location = Location(point=Point(1, 2, 3), labware=mock_well)

    subject.move_to(location)

    decoy.verify(
        mock_instrument_core.move_to(
            location=location,
            well_core=mock_well._core,
            force_direct=False,
            minimum_z_height=None,
            speed=None,
            check_for_movement_conflicts=False,
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 13)])
def test_pick_up_from_well(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should pick up a specific tip."""
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(1, 2, 3), labware=mock_well)

    decoy.when(mock_well.top()).then_return(top_location)

    subject.pick_up_tip(mock_well, presses=1, increment=2.0, prep_after=False)

    decoy.verify(
        mock_instrument_core.pick_up_tip(
            location=top_location,
            well_core=mock_well._core,
            presses=1,
            increment=2.0,
            prep_after=False,
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 14)])
def test_pick_up_from_well_deprecated_args(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should pick up a specific tip."""
    mock_well = decoy.mock(cls=Well)

    with pytest.raises(UnsupportedAPIError):
        subject.pick_up_tip(mock_well, presses=1, increment=2.0, prep_after=False)


def test_aspirate(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate to a well."""
    # Before PR #12105, this function tested calling aspirate(location=Well).
    # Then this function was changed to call aspirate(location=Location), but that's
    # the same thing that test_aspirate_well_location() is testing for.
    # So we're restoring this function to test for aspirate(location=Well).
    mock_well = decoy.mock(cls=Well)
    bottom_location = Location(point=Point(1, 2, 3), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_well.bottom(z=1.0)).then_return(bottom_location)
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)

    subject.aspirate(volume=42.0, location=mock_well, rate=1.23)

    decoy.verify(
        mock_instrument_core.aspirate(
            location=bottom_location,
            well_core=mock_well._core,
            in_place=False,
            volume=42.0,
            rate=1.23,
            flow_rate=5.67,
            meniscus_tracking=None,
        ),
        times=1,
    )


def test_aspirate_well_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate to a well."""
    mock_well = decoy.mock(cls=Well)
    input_location = Location(point=Point(2, 2, 2), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)

    subject.aspirate(volume=42.0, location=input_location, rate=1.23)

    decoy.verify(
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=mock_well._core,
            in_place=False,
            volume=42.0,
            rate=1.23,
            flow_rate=5.67,
            meniscus_tracking=None,
        ),
        times=1,
    )


def test_aspirate_meniscus_well_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate to a well."""
    mock_well = decoy.mock(cls=Well)
    input_location = Location(point=Point(2, 2, 2), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)

    subject.aspirate(volume=42.0, location=input_location, rate=1.23)

    decoy.verify(
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=mock_well._core,
            in_place=False,
            volume=42.0,
            rate=1.23,
            flow_rate=5.67,
            meniscus_tracking=None,
        ),
        times=1,
    )


def test_aspirate_from_coordinates(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate from given coordinates."""
    input_location = Location(point=Point(2, 2, 2), labware=None)
    last_location = input_location  # to demonstrate in_place=True
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)

    subject.aspirate(volume=42.0, location=input_location, rate=1.23)

    decoy.verify(
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=None,
            in_place=True,
            volume=42.0,
            rate=1.23,
            flow_rate=5.67,
            meniscus_tracking=None,
        ),
        times=1,
    )


def test_aspirate_raises_no_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """Should raise a RuntimeError error."""
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)
    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(None)

    with pytest.raises(RuntimeError):
        subject.aspirate(location=None)


def test_aspirate_flow_rate(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate with absolute_flow_rate."""
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_instrument_core.get_aspirate_flow_rate()).then_return(400)

    subject.aspirate(volume=30, flow_rate=600)

    decoy.verify(
        mock_instrument_core.aspirate(
            location=last_location,
            well_core=None,
            in_place=True,
            volume=30,
            rate=1.5,  # requested flow_rate is 1.5 times default of 400
            flow_rate=600,
            meniscus_tracking=None,
        ),
        times=1,
    )

    # Should raise if both `rate` and `flow_rate` are specified:
    with pytest.raises(ValueError):
        subject.aspirate(volume=30, rate=1.5, flow_rate=600)


def test_blow_out_to_well(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should blow out to a well."""
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(1, 2, 3), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_well.top()).then_return(top_location)
    subject.blow_out(location=mock_well)

    decoy.verify(
        mock_instrument_core.blow_out(
            location=top_location, well_core=mock_well._core, in_place=False
        ),
        times=1,
    )


def test_blow_out_to_well_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should blow out to a well location."""
    mock_well = decoy.mock(cls=Well)
    input_location = Location(point=Point(2, 2, 2), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    subject.blow_out(location=input_location)

    decoy.verify(
        mock_instrument_core.blow_out(
            location=input_location, well_core=mock_well._core, in_place=False
        ),
        times=1,
    )


def test_blow_out_to_well_meniscus_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should blow out to a well location."""
    liquid_height = 10.0
    well_bottom = Point(2, 2, 2)
    relative_height = 3
    mock_well = decoy.mock(cls=Well)
    input_location_absolute = Location(
        point=well_bottom + Point(0, 0, liquid_height) + Point(0, 0, relative_height),
        labware=mock_well,
    )
    decoy.when(mock_well.current_liquid_height()).then_return(liquid_height)
    decoy.when(mock_well.bottom(liquid_height + relative_height)).then_return(
        Location(
            point=well_bottom + Point(0, 0, liquid_height + relative_height),
            labware=mock_well,
        )
    )

    input_location = Location(
        point=Point(0, 0, relative_height),
        labware=mock_well,
        _meniscus_tracking=MeniscusTrackingTarget.END,
    )
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    subject.blow_out(location=input_location)

    mock_instrument_core.blow_out(
        location=input_location_absolute, well_core=mock_well._core, in_place=False
    )


def test_blow_out_to_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should blow out to a location."""
    input_location = Location(point=Point(2, 2, 2), labware=None)
    last_location = input_location  # to demonstrate how we set in_place=True
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )

    subject.blow_out(location=input_location)

    decoy.verify(
        mock_instrument_core.blow_out(
            location=input_location, well_core=None, in_place=True
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", versions_at_or_above(APIVersion(2, 24)))
def test_blow_out_with_trash_last_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should blow out into a previously accessed disposal location."""
    mock_chute = decoy.mock(cls=WasteChute)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.LEFT)
    decoy.when(mock_protocol_core.get_last_location(mount=Mount.LEFT)).then_return(
        mock_chute
    )
    subject.blow_out()

    decoy.verify(
        mock_instrument_core.blow_out(
            location=mock_chute, well_core=None, in_place=True
        ),
        times=1,
    )


@pytest.mark.parametrize(
    "api_version",
    versions_between(
        low_exclusive_bound=APIVersion(2, 13), high_inclusive_bound=APIVersion(2, 23)
    ),
)
def test_blow_out_with_trash_last_location_raises_earlier_api(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should raise if a trash is the last accessed location and on 2.23."""
    mock_trash = decoy.mock(cls=TrashBin)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.LEFT)
    decoy.when(mock_protocol_core.get_last_location(mount=Mount.LEFT)).then_return(
        mock_trash
    )
    with pytest.raises(
        RuntimeError, match="blow out is called without an explicit location"
    ):
        subject.blow_out()


def test_blow_out_raises_no_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """Should raise a RuntimeError."""
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)
    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(None)

    with pytest.raises(RuntimeError):
        subject.blow_out(location=None)


MOCK_MAP = NozzleMap.build(
    physical_nozzles=OrderedDict({"A1": Point(0, 0, 0)}),
    physical_rows=OrderedDict({"A": ["A1"]}),
    physical_columns=OrderedDict({"1": ["A1"]}),
    starting_nozzle="A1",
    back_left_nozzle="A1",
    front_right_nozzle="A1",
    valid_nozzle_maps=ValidNozzleMaps(maps={"Full": ["A1"]}),
)


@pytest.mark.parametrize(
    argnames=["api_version", "mock_map"],
    argvalues=[(APIVersion(2, 18), MOCK_MAP), (APIVersion(2, 17), None)],
)
def test_pick_up_tip_from_labware(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_map: Optional[NozzleMap],
) -> None:
    """It should pick up the next tip from a given labware."""
    mock_tip_rack = decoy.mock(cls=Labware)
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(1, 2, 3), labware=mock_well)

    decoy.when(mock_instrument_core.get_active_channels()).then_return(123)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(
        labware.next_available_tip(
            starting_tip=None,
            tip_racks=[mock_tip_rack],
            channels=123,
            nozzle_map=mock_map,
        )
    ).then_return((mock_tip_rack, mock_well))
    decoy.when(mock_well.top()).then_return(top_location)

    subject.pick_up_tip(mock_tip_rack)

    decoy.verify(
        mock_instrument_core.pick_up_tip(
            location=top_location,
            well_core=mock_well._core,
            presses=None,
            increment=None,
            prep_after=True,
        ),
        times=1,
    )


def test_pick_up_tip_from_well_location(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should pick up the next tip from a given well-based Location."""
    mock_well = decoy.mock(cls=Well)
    location = Location(point=Point(1, 2, 3), labware=mock_well)

    subject.pick_up_tip(location)

    decoy.verify(
        mock_instrument_core.pick_up_tip(
            location=location,
            well_core=mock_well._core,
            presses=None,
            increment=None,
            prep_after=True,
        ),
        times=1,
    )


@pytest.mark.parametrize(
    argnames=["api_version", "mock_map"],
    argvalues=[(APIVersion(2, 18), MOCK_MAP), (APIVersion(2, 17), None)],
)
def test_pick_up_tip_from_labware_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_map: Optional[NozzleMap],
) -> None:
    """It should pick up the next tip from a given labware-based Location."""
    mock_tip_rack = decoy.mock(cls=Labware)
    mock_well = decoy.mock(cls=Well)
    location = Location(point=Point(1, 2, 3), labware=mock_tip_rack)
    top_location = Location(point=Point(1, 2, 3), labware=mock_well)

    decoy.when(mock_instrument_core.get_active_channels()).then_return(123)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(
        labware.next_available_tip(
            starting_tip=None,
            tip_racks=[mock_tip_rack],
            channels=123,
            nozzle_map=mock_map,
        )
    ).then_return((mock_tip_rack, mock_well))
    decoy.when(mock_well.top()).then_return(top_location)

    subject.pick_up_tip(location)

    decoy.verify(
        mock_instrument_core.pick_up_tip(
            location=top_location,
            well_core=mock_well._core,
            presses=None,
            increment=None,
            prep_after=True,
        ),
        times=1,
    )


@pytest.mark.parametrize(
    argnames=["api_version", "mock_map"],
    argvalues=[(APIVersion(2, 18), MOCK_MAP), (APIVersion(2, 17), None)],
)
def test_pick_up_from_associated_tip_racks(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_map: Optional[NozzleMap],
) -> None:
    """It should pick up from its associated tip racks."""
    mock_tip_rack_1 = decoy.mock(cls=Labware)
    mock_tip_rack_2 = decoy.mock(cls=Labware)
    mock_starting_tip = decoy.mock(cls=Well)
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(1, 2, 3), labware=mock_well)

    decoy.when(mock_instrument_core.is_tip_tracking_available()).then_return(True)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(123)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(
        labware.next_available_tip(
            starting_tip=mock_starting_tip,
            tip_racks=[mock_tip_rack_1, mock_tip_rack_2],
            channels=123,
            nozzle_map=mock_map,
        )
    ).then_return((mock_tip_rack_2, mock_well))
    decoy.when(mock_well.top()).then_return(top_location)

    subject.starting_tip = mock_starting_tip
    subject.tip_racks = [mock_tip_rack_1, mock_tip_rack_2]
    subject.pick_up_tip()

    decoy.verify(
        mock_instrument_core.pick_up_tip(
            location=top_location,
            well_core=mock_well._core,
            presses=None,
            increment=None,
            prep_after=True,
        ),
        times=1,
    )


def test_pick_up_fails_when_tip_tracking_unavailable(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should raise an error if automatic tip tracking is not available.."""
    mock_tip_rack_1 = decoy.mock(cls=Labware)

    decoy.when(mock_instrument_core.is_tip_tracking_available()).then_return(False)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(123)

    subject.tip_racks = [mock_tip_rack_1]
    with pytest.raises(
        CommandPreconditionViolated, match="Automatic tip tracking is not available"
    ):
        subject.pick_up_tip()


def test_drop_tip_to_well(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should drop a tip in a specific well."""
    mock_well = decoy.mock(cls=Well)

    subject.drop_tip(mock_well, home_after=False)

    decoy.verify(
        mock_instrument_core.drop_tip(
            location=None,
            well_core=mock_well._core,
            home_after=False,
            alternate_drop_location=False,
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 14)])
def test_drop_tip_to_trash(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_trash: Labware,
    subject: InstrumentContext,
) -> None:
    """It should drop a tip in the trash if not given a location ."""
    mock_well = decoy.mock(cls=Well)

    decoy.when(mock_trash.wells()).then_return([mock_well])

    subject.drop_tip()

    decoy.verify(
        mock_instrument_core.drop_tip(
            location=None,
            well_core=mock_well._core,
            home_after=None,
            alternate_drop_location=False,
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 15)])
def test_drop_tip_to_randomized_trash_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_trash: Labware,
    subject: InstrumentContext,
) -> None:
    """It should drop a tip in the trash if not given a location ."""
    mock_well = decoy.mock(cls=Well)

    decoy.when(mock_trash.wells()).then_return([mock_well])

    subject.drop_tip()

    decoy.verify(
        mock_instrument_core.drop_tip(
            location=None,
            well_core=mock_well._core,
            home_after=None,
            alternate_drop_location=True,
        ),
        times=1,
    )


@pytest.mark.parametrize(
    ["api_version", "alternate_drop"],
    [(APIVersion(2, 17), True), (APIVersion(2, 18), False)],
)
def test_drop_tip_in_trash_bin(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    alternate_drop: bool,
    subject: InstrumentContext,
) -> None:
    """It should drop a tip in a deck configured trash bin."""
    trash_bin = decoy.mock(cls=TrashBin)

    subject.drop_tip(trash_bin)

    decoy.verify(
        mock_instrument_core.drop_tip_in_disposal_location(
            trash_bin,
            home_after=None,
            alternate_tip_drop=alternate_drop,
        ),
        times=1,
    )


@pytest.mark.parametrize(
    ["api_version", "alternate_drop"],
    [(APIVersion(2, 17), True), (APIVersion(2, 18), False)],
)
def test_drop_tip_in_waste_chute(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    alternate_drop: bool,
    subject: InstrumentContext,
) -> None:
    """It should drop a tip in a deck configured trash bin or waste chute."""
    waste_chute = decoy.mock(cls=WasteChute)

    subject.drop_tip(waste_chute)

    decoy.verify(
        mock_instrument_core.drop_tip_in_disposal_location(
            waste_chute,
            home_after=None,
            alternate_tip_drop=alternate_drop,
        ),
        times=1,
    )


def test_drop_tip_in_disposal_location_implicitly(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
) -> None:
    """It should drop a tip in a deck configured trash bin when no arguments have been provided."""
    trash_bin = decoy.mock(cls=TrashBin)
    subject.trash_container = trash_bin

    subject.drop_tip()

    decoy.verify(
        mock_instrument_core.drop_tip_in_disposal_location(
            trash_bin,
            home_after=None,
            alternate_tip_drop=True,
        ),
        times=1,
    )


def test_return_tip(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should pick up a tip and return it."""
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(1, 2, 3), labware=mock_well)
    decoy.when(mock_well.top()).then_return(top_location)

    subject.pick_up_tip(mock_well)
    subject.return_tip()

    decoy.verify(
        mock_instrument_core.pick_up_tip(
            location=top_location,
            well_core=mock_well._core,
            presses=None,
            increment=None,
            prep_after=True,
        ),
        mock_instrument_core.drop_tip(
            location=None,
            well_core=mock_well._core,
            home_after=None,
            alternate_drop_location=False,
        ),
    )

    with pytest.raises(TypeError, match="Last tip location"):
        subject.return_tip()


def test_dispense_with_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should dispense to a given location."""
    input_location = Location(point=Point(2, 2, 2), labware=None)
    last_location = input_location  # to demonstrate in_place=True
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1.23)).then_return(5.67)

    subject.dispense(volume=42.0, location=input_location, rate=1.23)

    decoy.verify(
        mock_instrument_core.dispense(
            location=input_location,
            well_core=None,
            in_place=True,
            volume=42.0,
            rate=1.23,
            flow_rate=5.67,
            push_out=None,
            meniscus_tracking=None,
        ),
        times=1,
    )


def test_dispense_with_well_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should dispense to a well location."""
    mock_well = decoy.mock(cls=Well)
    input_location = Location(point=Point(2, 2, 2), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1.23)).then_return(3.0)

    subject.dispense(volume=42.0, location=input_location, rate=1.23, push_out=7)

    decoy.verify(
        mock_instrument_core.dispense(
            location=input_location,
            well_core=mock_well._core,
            in_place=False,
            volume=42.0,
            rate=1.23,
            flow_rate=3.0,
            push_out=7,
            meniscus_tracking=None,
        ),
        times=1,
    )


def test_dispense_with_well(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should dispense to a well."""
    mock_well = decoy.mock(cls=Well)
    bottom_location = Location(point=Point(1, 2, 3), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_well.bottom(z=1.0)).then_return(bottom_location)
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1.23)).then_return(5.67)

    subject.dispense(volume=42.0, location=mock_well, rate=1.23, push_out=None)

    decoy.verify(
        mock_instrument_core.dispense(
            location=bottom_location,
            well_core=mock_well._core,
            in_place=False,
            volume=42.0,
            rate=1.23,
            flow_rate=5.67,
            push_out=None,
            meniscus_tracking=None,
        ),
        times=1,
    )


def test_dispense_raises_no_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """Should raise a RuntimeError."""
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)
    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(None)

    with pytest.raises(RuntimeError):
        subject.dispense(location=None)


@pytest.mark.parametrize("api_version", [APIVersion(2, 14)])
def test_dispense_push_out_on_not_allowed_version(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """Should raise a APIVersionError."""
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)
    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(None)

    with pytest.raises(APIVersionError):
        subject.dispense(push_out=3)


def test_dispense_flow_rate(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should dispense with absolute_flow_rate."""
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )
    decoy.when(mock_instrument_core.get_dispense_flow_rate()).then_return(400)

    subject.dispense(volume=30, flow_rate=600)

    decoy.verify(
        mock_instrument_core.dispense(
            location=last_location,
            well_core=None,
            volume=30,
            rate=1.5,  # requested flow_rate is 1.5 times default of 400
            flow_rate=600,
            in_place=True,
            push_out=None,
            meniscus_tracking=None,
        ),
        times=1,
    )

    # Should raise if both `rate` and `flow_rate` are specified:
    with pytest.raises(ValueError):
        subject.dispense(volume=30, rate=1.5, flow_rate=600)


def test_touch_tip(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
) -> None:
    """It should touch the pipette tip to the edges of the well with the core."""
    mock_well = decoy.mock(cls=Well)

    decoy.when(mock_instrument_core.has_tip()).then_return(True)

    decoy.when(mock_well.top(z=4.56)).then_return(
        Location(point=Point(1, 2, 3), labware=mock_well)
    )

    decoy.when(mock_well.parent.quirks).then_return([])

    # touch_tip() with the old `radius` argument:

    subject.touch_tip(mock_well, radius=0.123, v_offset=4.56, speed=42.0)

    decoy.verify(
        mock_instrument_core.touch_tip(
            location=Location(point=Point(1, 2, 3), labware=mock_well),
            well_core=mock_well._core,
            radius=0.123,
            z_offset=4.56,
            speed=42.0,
            mm_from_edge=None,
        )
    )

    # touch_tip() with the new `mm_from_edge` argument:

    subject.touch_tip(mock_well, v_offset=4.56, speed=42.0, mm_from_edge=0.5)

    decoy.verify(
        mock_instrument_core.touch_tip(
            location=Location(point=Point(1, 2, 3), labware=mock_well),
            well_core=mock_well._core,
            radius=1,
            z_offset=4.56,
            speed=42.0,
            mm_from_edge=0.5,
        )
    )

    # `radius` and `mm_from_edge` are mutually exclusive, should raise if both specified:
    with pytest.raises(ValueError):
        subject.touch_tip(
            mock_well, radius=0.75, v_offset=4.56, speed=42.0, mm_from_edge=0.5
        )


def test_touch_tip_raises_if_trash_last_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should raise if the last location was a trash bin or waste chute."""
    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    mock_trash = decoy.mock(cls=TrashBin)
    decoy.when(mock_protocol_core.get_last_location()).then_return(mock_trash)
    with pytest.raises(RuntimeError, match="not valid for touch tip"):
        subject.touch_tip()

    mock_chute = decoy.mock(cls=WasteChute)
    decoy.when(mock_protocol_core.get_last_location()).then_return(mock_chute)
    with pytest.raises(RuntimeError, match="not valid for touch tip"):
        subject.touch_tip()


def test_return_height(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should get the tip return scale factor."""
    decoy.when(mock_instrument_core.get_return_height()).then_return(0.123)

    result = subject.return_height

    assert result == 0.123


def test_flow_rate(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should return a FlowRates object."""
    flow_rates = decoy.mock(cls=FlowRates)
    decoy.when(mock_instrument_core.get_flow_rate()).then_return(flow_rates)

    result = subject.flow_rate

    assert result == flow_rates


def test_liquid_presence_detection(
    decoy: Decoy, mock_instrument_core: InstrumentCore, subject: InstrumentContext
) -> None:
    """It should have a default liquid presence detection boolean set to False."""
    decoy.when(mock_instrument_core.get_liquid_presence_detection()).then_return(False)
    assert subject.liquid_presence_detection is False
    subject.liquid_presence_detection = True
    decoy.verify(mock_instrument_core.set_liquid_presence_detection(True), times=1)


@pytest.mark.parametrize("api_version", [APIVersion(2, 13)])
@pytest.mark.parametrize(
    "mock_instrument_core",
    [lazy_fixture("mock_legacy_instrument_core")],
)
def test_plunger_speed(
    decoy: Decoy,
    mock_legacy_instrument_core: LegacyInstrumentCore,
    subject: InstrumentContext,
) -> None:
    """It should return a PlungerSpeeds object on PAPI <= v2.13."""
    plunger_speeds = decoy.mock(cls=PlungerSpeeds)
    decoy.when(mock_legacy_instrument_core.get_speed()).then_return(plunger_speeds)

    result = subject.speed

    assert result == plunger_speeds


@pytest.mark.parametrize("api_version", [APIVersion(2, 14)])
def test_plunger_speed_removed(subject: InstrumentContext) -> None:
    """It should raise an error on PAPI >= v2.14."""
    with pytest.raises(UnsupportedAPIError):
        subject.speed


def test_prepare_to_aspirate(
    subject: InstrumentContext, decoy: Decoy, mock_instrument_core: InstrumentCore
) -> None:
    """It should call the core function."""
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    subject.prepare_to_aspirate()
    decoy.verify(mock_instrument_core.prepare_to_aspirate(), times=1)


def test_prepare_to_aspirate_checks_volume(
    subject: InstrumentContext, decoy: Decoy, mock_instrument_core: InstrumentCore
) -> None:
    """It should raise an error if you prepare for aspirate with liquid in the pipette."""
    decoy.when(mock_instrument_core.get_current_volume()).then_return(10)
    with pytest.raises(CommandPreconditionViolated):
        subject.prepare_to_aspirate()


@pytest.mark.parametrize(
    argnames=PipetteReliantNozzleConfigSpec._fields,
    argvalues=PIPETTE_RELIANT_TEST_SPECS,
)
def test_configure_pip_reliant_nozzle_layout_checks_for_config_validity(
    subject: InstrumentContext,
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    pipette_channels: int,
    nozzle_layout_args: NozzleLayoutArgs,
    expected_raise: ContextManager[Any],
) -> None:
    """It should raise an error if you specify the wrong arguments for the nozzle configuration."""
    decoy.when(mock_instrument_core.get_channels()).then_return(pipette_channels)
    with expected_raise:
        subject.configure_nozzle_layout(
            style=nozzle_layout_args.style,
            start=nozzle_layout_args.start,
            end=nozzle_layout_args.end,
            front_right=nozzle_layout_args.front_right,
            back_left=nozzle_layout_args.back_left,
        )


@pytest.mark.parametrize(
    "pipette_channels",
    [1, 8, 96],
)
@pytest.mark.parametrize(
    argnames=PipetteIndependentNozzleConfigSpec._fields,
    argvalues=PIPETTE_INDEPENDENT_TEST_SPECS,
)
def test_configure_pip_independent_nozzle_layout_checks_for_config_validity(
    subject: InstrumentContext,
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    pipette_channels: int,
    nozzle_layout_args: NozzleLayoutArgs,
    expected_raise: ContextManager[Any],
) -> None:
    """It should raise an error if you specify the wrong arguments for the nozzle configuration."""
    decoy.when(mock_instrument_core.get_channels()).then_return(pipette_channels)
    with expected_raise:
        subject.configure_nozzle_layout(
            style=nozzle_layout_args.style,
            start=nozzle_layout_args.start,
            end=nozzle_layout_args.end,
            front_right=nozzle_layout_args.front_right,
            back_left=nozzle_layout_args.back_left,
        )


@pytest.mark.parametrize(
    argnames=InstrumentCoreNozzleConfigSpec._fields,
    argvalues=INSTRUMENT_CORE_NOZZLE_LAYOUT_TEST_SPECS,
)
def test_configure_nozzle_layout(
    subject: InstrumentContext,
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    pipette_channels: int,
    nozzle_layout_args: NozzleLayoutArgs,
    expected_core_args: ExpectedCoreArgs,
) -> None:
    """It should pass the correct configuration model to the engine client."""
    decoy.when(mock_instrument_core.get_channels()).then_return(pipette_channels)
    subject.configure_nozzle_layout(
        style=nozzle_layout_args.style,
        start=nozzle_layout_args.start,
        end=nozzle_layout_args.end,
        front_right=nozzle_layout_args.front_right,
        back_left=nozzle_layout_args.back_left,
    )
    decoy.verify(
        mock_instrument_core.configure_nozzle_layout(
            style=nozzle_layout_args.style,
            primary_nozzle=expected_core_args.primary_nozzle,
            front_right_nozzle=expected_core_args.front_right_nozzle,
            back_left_nozzle=expected_core_args.back_left_nozzle,
        )
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 15)])
def test_dispense_0_volume_means_dispense_everything(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should dispense all liquid to a well."""
    input_location = Location(point=Point(2, 2, 2), labware=None)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(100)
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1.23)).then_return(5.67)
    subject.dispense(volume=0, location=input_location, rate=1.23, push_out=None)

    decoy.verify(
        mock_instrument_core.dispense(
            location=input_location,
            well_core=None,
            in_place=False,
            volume=100,
            rate=1.23,
            flow_rate=5.67,
            push_out=None,
            meniscus_tracking=None,
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 16)])
def test_dispense_0_volume_means_dispense_nothing(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should dispense no liquid to a well."""
    input_location = Location(point=Point(2, 2, 2), labware=None)
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1.23)).then_return(5.67)
    subject.dispense(volume=0, location=input_location, rate=1.23, push_out=None)

    decoy.verify(
        mock_instrument_core.dispense(
            location=input_location,
            well_core=None,
            in_place=False,
            volume=0,
            rate=1.23,
            flow_rate=5.67,
            push_out=None,
            meniscus_tracking=None,
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 15)])
def test_aspirate_0_volume_means_aspirate_everything(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate to a well."""
    mock_well = decoy.mock(cls=Well)
    input_location = Location(point=Point(2, 2, 2), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )

    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)
    decoy.when(mock_instrument_core.get_available_volume()).then_return(200)
    subject.aspirate(volume=0, location=input_location, rate=1.23)

    decoy.verify(
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=mock_well._core,
            in_place=False,
            volume=200,
            rate=1.23,
            flow_rate=5.67,
            meniscus_tracking=None,
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", [APIVersion(2, 16)])
def test_aspirate_0_volume_means_aspirate_nothing(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate to a well."""
    mock_well = decoy.mock(cls=Well)
    input_location = Location(point=Point(2, 2, 2), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=None)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.RIGHT)

    decoy.when(mock_protocol_core.get_last_location(Mount.RIGHT)).then_return(
        last_location
    )

    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)

    subject.aspirate(volume=0, location=input_location, rate=1.23)

    decoy.verify(
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=mock_well._core,
            in_place=False,
            volume=0,
            rate=1.23,
            flow_rate=5.67,
            meniscus_tracking=None,
        ),
        times=1,
    )


@pytest.mark.parametrize("api_version", versions_at_or_above(APIVersion(2, 24)))
def test_dispense_with_trash_last_location(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should dispense into a previously accessed trash."""
    mock_trash = decoy.mock(cls=TrashBin)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.LEFT)
    decoy.when(mock_protocol_core.get_last_location(mount=Mount.LEFT)).then_return(
        mock_trash
    )
    decoy.when(mock_instrument_core.get_dispense_flow_rate(4.5)).then_return(6.7)
    subject.dispense(volume=12.3, rate=4.5)

    decoy.verify(
        mock_instrument_core.dispense(
            location=mock_trash,
            well_core=None,
            in_place=True,
            volume=12.3,
            rate=4.5,
            flow_rate=6.7,
            push_out=None,
            meniscus_tracking=None,
        ),
        times=1,
    )


@pytest.mark.parametrize(
    "api_version",
    versions_between(
        low_exclusive_bound=APIVersion(2, 13), high_inclusive_bound=APIVersion(2, 23)
    ),
)
def test_dispense_with_trash_last_location_raises_earlier_api(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should raise if a trash is the last accessed location and on 2.23."""
    mock_trash = decoy.mock(cls=TrashBin)
    decoy.when(mock_instrument_core.get_mount()).then_return(Mount.LEFT)
    decoy.when(mock_protocol_core.get_last_location(mount=Mount.LEFT)).then_return(
        mock_trash
    )
    with pytest.raises(
        RuntimeError, match="dispense is called without an explicit location"
    ):
        subject.dispense(volume=12.3, rate=4.5)


@pytest.mark.parametrize("api_version", [APIVersion(2, 20)])
def test_detect_liquid_presence(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should only return booleans. Not raise an exception."""
    mock_well = decoy.mock(cls=Well)
    decoy.when(
        mock_instrument_core.detect_liquid_presence(mock_well._core, mock_well.top())
    ).then_return(sentinel.inner_result)
    outer_result = subject.detect_liquid_presence(mock_well)
    assert outer_result is sentinel.inner_result


@pytest.mark.parametrize("api_version", [APIVersion(2, 20)])
def test_require_liquid_presence(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should raise an exception when called."""
    mock_well = decoy.mock(cls=Well)
    lnfe = LiquidNotFoundError(id="1234", createdAt=datetime.now())
    errorToRaise = ProtocolCommandFailedError(
        original_error=lnfe,
        message=f"{lnfe.errorType}: {lnfe.detail}",
    )
    decoy.when(
        mock_instrument_core.liquid_probe_with_recovery(
            mock_well._core, mock_well.top()
        )
    )
    subject.require_liquid_presence(mock_well)
    decoy.when(
        mock_instrument_core.liquid_probe_with_recovery(
            mock_well._core, mock_well.top()
        )
    ).then_raise(errorToRaise)
    with pytest.raises(ProtocolCommandFailedError) as pcfe:
        subject.require_liquid_presence(mock_well)
    assert pcfe.value is errorToRaise


@pytest.mark.parametrize("api_version", [APIVersion(2, 20)])
def test_measure_liquid_height(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should raise an exception when called."""
    mock_well = decoy.mock(cls=Well)
    lnfe = LiquidNotFoundError(id="1234", createdAt=datetime.now())
    errorToRaise = ProtocolCommandFailedError(
        original_error=lnfe,
        message=f"{lnfe.errorType}: {lnfe.detail}",
    )
    decoy.when(mock_well.current_liquid_height()).then_return(123)
    decoy.when(
        mock_instrument_core.liquid_probe_with_recovery(
            mock_well._core, mock_well.top()
        )
    )
    assert subject.measure_liquid_height(mock_well) == 123
    decoy.when(
        mock_instrument_core.liquid_probe_with_recovery(
            mock_well._core, mock_well.top()
        )
    ).then_raise(errorToRaise)
    with pytest.raises(ProtocolCommandFailedError) as pcfe:
        subject.measure_liquid_height(mock_well)
    assert pcfe.value is errorToRaise


@pytest.mark.parametrize(
    "api_version",
    versions_between(
        low_exclusive_bound=APIVersion(2, 13), high_inclusive_bound=APIVersion(2, 21)
    ),
)
def test_mix_no_lpd(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate/dispense to a well several times."""
    mock_well = decoy.mock(cls=Well)

    bottom_location = Location(point=Point(1, 2, 3), labware=mock_well)
    top_location = Location(point=Point(3, 2, 1), labware=None)
    last_location = Location(point=Point(9, 9, 9), labware=None)

    decoy.when(mock_protocol_core.get_last_location(Mount.LEFT)).then_return(
        # We start at last_location, then we're at bottom_location for the
        # subsequent dispenses/aspirates
        last_location,
        bottom_location,
    )
    decoy.when(mock_well.bottom(z=1.0)).then_return(bottom_location)
    decoy.when(mock_well.top()).then_return(top_location)
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1.23)).then_return(5.67)
    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0.0)

    subject.mix(repetitions=10, volume=10.0, location=mock_well, rate=1.23)
    decoy.verify(
        mock_instrument_core.aspirate(
            bottom_location,
            mock_well._core,
            10.0,
            1.23,
            5.67,
            matchers.Anything(),  # first one is not in_place, the other 9 are in_place
            None,
        ),
        times=10,
    )

    # Slight differences in dispense push-out logic for 2.14 and 2.15 api levels
    if subject.api_version < APIVersion(2, 16):
        decoy.verify(
            mock_instrument_core.dispense(
                bottom_location,
                mock_well._core,
                10.0,
                1.23,
                5.67,
                True,
                None,
                None,
            ),
            times=10,
        )
    else:
        decoy.verify(
            mock_instrument_core.dispense(
                bottom_location,
                mock_well._core,
                10.0,
                1.23,
                5.67,
                True,
                0.0,
                None,
            ),
            times=9,
        )
        decoy.verify(
            mock_instrument_core.dispense(
                bottom_location,
                mock_well._core,
                10.0,
                1.23,
                5.67,
                True,
                None,
                None,
            ),
            times=1,
        )

    decoy.verify(
        mock_instrument_core.liquid_probe_with_recovery(mock_well._core, top_location),
        times=0,
    )


@pytest.mark.ot3_only
@pytest.mark.parametrize("api_version", versions_at_or_above(APIVersion(2, 21)))
def test_mix_with_lpd(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should aspirate/dispense to a well several times and do 1 lpd."""
    mock_well = decoy.mock(cls=Well)
    bottom_location = Location(point=Point(1, 2, 3), labware=mock_well)
    top_location = Location(point=Point(3, 2, 1), labware=None)
    last_location = Location(point=Point(9, 9, 9), labware=None)

    decoy.when(mock_protocol_core.get_last_location(Mount.LEFT)).then_return(
        # We start at last_location, then we're at bottom_location for the
        # subsequent dispenses/aspirates
        last_location,
        bottom_location,
    )
    decoy.when(mock_well.bottom(z=1.0)).then_return(bottom_location)
    decoy.when(mock_well.top()).then_return(top_location)
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1.23)).then_return(5.67)
    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_instrument_core.get_has_clean_tip()).then_return(True)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0.0)
    decoy.when(mock_instrument_core.nozzle_configuration_valid_for_lld()).then_return(
        True
    )

    subject.liquid_presence_detection = True
    subject.mix(repetitions=10, volume=10.0, location=mock_well, rate=1.23)
    decoy.verify(
        mock_instrument_core.aspirate(
            bottom_location,
            mock_well._core,
            10.0,
            1.23,
            5.67,
            matchers.Anything(),  # first one is not in_place, the other 9 are in_place
            None,
        ),
        times=10,
    )
    decoy.verify(
        mock_instrument_core.dispense(
            bottom_location,
            mock_well._core,
            10.0,
            1.23,
            5.67,
            True,
            0.0,
            None,
        ),
        times=9,
    )
    decoy.verify(
        mock_instrument_core.dispense(
            bottom_location,
            mock_well._core,
            10.0,
            1.23,
            5.67,
            True,
            None,
            None,
        ),
        times=1,
    )
    decoy.verify(
        mock_instrument_core.liquid_probe_with_recovery(mock_well._core, top_location),
        times=1,
    )


def test_mix_with_flow_rates(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should mix with aspirate_flow_rate and dispense_flow_rate."""
    mock_well = decoy.mock(cls=Well)
    input_location = Location(point=Point(2, 2, 2), labware=mock_well)
    decoy.when(mock_protocol_core.get_last_location(Mount.LEFT)).then_return(
        input_location,
    )  # last location same as input_location, so in_place should be true
    decoy.when(mock_instrument_core.get_aspirate_flow_rate()).then_return(100.0)
    decoy.when(mock_instrument_core.get_dispense_flow_rate()).then_return(100.0)
    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0.0)

    subject.mix(
        repetitions=1,
        volume=10.0,
        location=input_location,
        aspirate_flow_rate=300.0,
        dispense_flow_rate=400.0,
    )
    decoy.verify(
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=mock_well._core,
            volume=10.0,
            rate=3.0,  # requested aspirate_flow_rate is 3x default flow rate of 100
            flow_rate=300.0,
            in_place=True,
            meniscus_tracking=None,
        ),
        mock_instrument_core.dispense(
            location=input_location,
            well_core=mock_well._core,
            volume=10.0,
            rate=4.0,  # requested dispense_flow_rate is 4x default flow rate of 100
            flow_rate=400.0,
            in_place=True,
            push_out=None,
            meniscus_tracking=None,
        ),
    )

    # Should fail if you try to set both rate and aspirate_flow_rate/dispense_flow_rate:
    with pytest.raises(ValueError):
        subject.mix(
            repetitions=1,
            volume=10.0,
            location=input_location,
            rate=1.23,
            aspirate_flow_rate=300.0,
            dispense_flow_rate=400.0,
        )

    # Bonus: If you only set aspirate_flow_rate, the dispense should use the pipette's
    # default flow rate:
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1)).then_return(100.0)
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1)).then_return(100.0)
    subject.mix(
        repetitions=1,
        volume=10.0,
        location=input_location,
        aspirate_flow_rate=300.0,
    )
    decoy.verify(
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=mock_well._core,
            volume=10.0,
            rate=3.0,  # requested aspirate_flow_rate is 3x default flow rate of 100
            flow_rate=300.0,
            in_place=True,
            meniscus_tracking=None,
        ),
        mock_instrument_core.dispense(
            location=input_location,
            well_core=mock_well._core,
            volume=10.0,
            rate=1.0,
            flow_rate=100.0,  # the default dispense flow rate
            in_place=True,
            push_out=None,
            meniscus_tracking=None,
        ),
    )


def test_mix_with_delay_and_final_push_out(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
) -> None:
    """It should delay after the aspirate/dispense and emit the specified push out for the final dispense in a mix."""
    mock_well = decoy.mock(cls=Well)
    input_location = Location(point=Point(2, 2, 2), labware=mock_well)
    decoy.when(mock_protocol_core.get_last_location(Mount.LEFT)).then_return(
        input_location,
    )  # last location same as input_location, so in_place should be true
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1)).then_return(4.56)
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1)).then_return(5.67)
    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0.0)

    subject.mix(
        repetitions=2,
        volume=10.0,
        location=input_location,
        aspirate_delay=3,
        dispense_delay=4,
        final_push_out=2,
    )
    decoy.verify(
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=mock_well._core,
            volume=10.0,
            rate=1,
            flow_rate=4.56,
            in_place=True,
            meniscus_tracking=None,
        ),
        mock_protocol_core.delay(3, msg=None),  # aspirate delay
        mock_instrument_core.dispense(
            location=input_location,
            well_core=mock_well._core,
            volume=10.0,
            rate=1,
            flow_rate=5.67,
            in_place=True,
            push_out=0.0,
            meniscus_tracking=None,
        ),
        mock_protocol_core.delay(4, msg=None),  # dispense delay
        mock_instrument_core.aspirate(
            location=input_location,
            well_core=mock_well._core,
            volume=10.0,
            rate=1,
            flow_rate=4.56,
            in_place=True,
            meniscus_tracking=None,
        ),
        mock_protocol_core.delay(3, msg=None),  # aspirate delay
        mock_instrument_core.dispense(
            location=input_location,
            well_core=mock_well._core,
            volume=10.0,
            rate=1,
            flow_rate=5.67,
            in_place=True,
            push_out=2,  # final push out
            meniscus_tracking=None,
        ),
        mock_protocol_core.delay(4, msg=None),  # dispense delay
    )


@pytest.mark.ot3_only
@pytest.mark.parametrize("clean,expected", [(True, 1), (False, 0)])
def test_aspirate_with_lpd(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    mock_protocol_core: ProtocolCore,
    clean: bool,
    expected: int,
) -> None:
    """It should aspirate/dispense to a well several times and do 1 lpd."""
    mock_well = decoy.mock(cls=Well)
    bottom_location = Location(point=Point(1, 2, 3), labware=mock_well)
    top_location = Location(point=Point(3, 2, 1), labware=None)
    last_location = Location(point=Point(9, 9, 9), labware=None)

    decoy.when(mock_protocol_core.get_last_location(Mount.LEFT)).then_return(
        last_location
    )
    decoy.when(mock_well.bottom(z=1.0)).then_return(bottom_location)
    decoy.when(mock_well.top()).then_return(top_location)
    decoy.when(mock_instrument_core.get_aspirate_flow_rate(1.23)).then_return(5.67)
    decoy.when(mock_instrument_core.get_dispense_flow_rate(1.23)).then_return(5.67)
    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_instrument_core.get_has_clean_tip()).then_return(clean)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0.0)
    decoy.when(mock_instrument_core.nozzle_configuration_valid_for_lld()).then_return(
        True
    )

    subject.liquid_presence_detection = True
    subject.aspirate(volume=10.0, location=mock_well, rate=1.23)
    decoy.verify(
        mock_instrument_core.aspirate(
            bottom_location,
            mock_well._core,
            10.0,
            1.23,
            5.67,
            False,
            None,
        ),
        times=1,
    )
    decoy.verify(
        mock_instrument_core.liquid_probe_with_recovery(mock_well._core, top_location),
        times=expected,
    )


@pytest.mark.parametrize(
    "api_version",
    versions_between(
        low_exclusive_bound=APIVersion(2, 13), high_inclusive_bound=APIVersion(2, 21)
    ),
)
def test_air_gap_uses_aspirate(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_protocol_core: ProtocolCore,
    subject: InstrumentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It should use its own aspirate function to aspirate air."""
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(9, 9, 14), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=mock_well)
    mock_aspirate = decoy.mock(func=subject.aspirate)
    mock_move_to = decoy.mock(func=subject.move_to)
    monkeypatch.setattr(subject, "aspirate", mock_aspirate)
    monkeypatch.setattr(subject, "move_to", mock_move_to)

    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_protocol_core.get_last_location()).then_return(last_location)
    decoy.when(mock_well.top(z=5.0)).then_return(top_location)
    subject.air_gap(volume=10, height=5)

    decoy.verify(mock_move_to(top_location, publish=False))
    decoy.verify(mock_aspirate(10))


@pytest.mark.parametrize("api_version", versions_at_or_above(APIVersion(2, 22)))
def test_air_gap_uses_air_gap(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_protocol_core: ProtocolCore,
    subject: InstrumentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It should use its own aspirate function to aspirate air."""
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(9, 9, 14), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=mock_well)
    mock_move_to = decoy.mock(func=subject.move_to)
    monkeypatch.setattr(subject, "move_to", mock_move_to)

    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_protocol_core.get_last_location()).then_return(last_location)
    decoy.when(mock_well.top(z=5.0)).then_return(top_location)
    decoy.when(mock_instrument_core.get_aspirate_flow_rate()).then_return(11)

    subject.air_gap(volume=10, height=5)

    decoy.verify(mock_move_to(top_location, publish=False))
    decoy.verify(mock_instrument_core.prepare_to_aspirate())
    decoy.verify(mock_instrument_core.air_gap_in_place(10, 11))


def test_air_gap_in_place(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It should air gap in place when in_place=True."""
    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_instrument_core.get_aspirate_flow_rate()).then_return(11)
    monkeypatch.setattr(subject, "move_to", None)  # pipette should not move

    subject.air_gap(volume=10, in_place=True)

    decoy.verify(mock_instrument_core.air_gap_in_place(10, 11))

    # Should not allow height if in_place=True is specified.
    with pytest.raises(ValueError):
        subject.air_gap(volume=10, height=2, in_place=True)
    # height=0 is also not allowed when in_place=True.
    with pytest.raises(ValueError):
        subject.air_gap(volume=10, height=0, in_place=True)


@pytest.mark.parametrize("api_version", versions_at_or_above(APIVersion(2, 24)))
def test_air_gap_has_rate(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_protocol_core: ProtocolCore,
    subject: InstrumentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It should use its own rate param."""
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(9, 9, 14), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=mock_well)
    mock_move_to = decoy.mock(func=subject.move_to)
    monkeypatch.setattr(subject, "move_to", mock_move_to)

    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_protocol_core.get_last_location()).then_return(last_location)
    decoy.when(mock_well.top(z=5.0)).then_return(top_location)
    decoy.when(mock_instrument_core.get_aspirate_flow_rate()).then_return(100)

    subject.air_gap(volume=10, height=5, rate=1.2)

    decoy.verify(mock_move_to(top_location, publish=False))
    decoy.verify(mock_instrument_core.prepare_to_aspirate())
    decoy.verify(
        mock_instrument_core.air_gap_in_place(10, 120)
    )  # 120 is from the flow_rate calculated from the rate * aspirate_flow_rate param


@pytest.mark.parametrize("api_version", versions_at_or_above(APIVersion(2, 24)))
def test_air_gap_has_flow_rate(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_protocol_core: ProtocolCore,
    subject: InstrumentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It should use its own flow_rate param."""
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(9, 9, 14), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=mock_well)
    mock_move_to = decoy.mock(func=subject.move_to)
    monkeypatch.setattr(subject, "move_to", mock_move_to)

    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_protocol_core.get_last_location()).then_return(last_location)
    decoy.when(mock_well.top(z=5.0)).then_return(top_location)

    subject.air_gap(volume=10, height=5, flow_rate=100)

    decoy.verify(mock_move_to(top_location, publish=False))
    decoy.verify(mock_instrument_core.prepare_to_aspirate())
    decoy.verify(
        mock_instrument_core.air_gap_in_place(10, 100)
    )  # 100 is the flow_rate param


def test_air_gap_has_flow_rate_and_rate(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_protocol_core: ProtocolCore,
    subject: InstrumentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It raise an error when rate and flow_rate are specified."""
    mock_well = decoy.mock(cls=Well)
    top_location = Location(point=Point(9, 9, 14), labware=mock_well)
    last_location = Location(point=Point(9, 9, 9), labware=mock_well)
    mock_move_to = decoy.mock(func=subject.move_to)
    monkeypatch.setattr(subject, "move_to", mock_move_to)

    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_protocol_core.get_last_location()).then_return(last_location)
    decoy.when(mock_well.top(z=5.0)).then_return(top_location)
    with pytest.raises(ValueError, match="Cannot define both flow_rate and rate."):
        subject.air_gap(volume=10, height=5, flow_rate=100, rate=5.0)


@pytest.mark.parametrize("api_version", versions_at_or_above(APIVersion(2, 24)))
def test_air_gap_over_trash(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_protocol_core: ProtocolCore,
    subject: InstrumentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It should air gap over a disposal location."""
    mock_trash = decoy.mock(cls=TrashBin)
    mock_trash_2 = decoy.mock(cls=TrashBin)
    mock_move_to = decoy.mock(func=subject.move_to)
    monkeypatch.setattr(subject, "move_to", mock_move_to)

    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_protocol_core.get_last_location()).then_return(mock_trash)
    decoy.when(mock_instrument_core.get_aspirate_flow_rate()).then_return(11)
    decoy.when(mock_trash.top(11)).then_return(mock_trash_2)

    subject.air_gap(volume=10, height=11)

    decoy.verify(mock_move_to(mock_trash_2, publish=False))
    decoy.verify(mock_instrument_core.prepare_to_aspirate())
    decoy.verify(mock_instrument_core.air_gap_in_place(10, 11))


@pytest.mark.parametrize(
    "api_version",
    versions_between(
        low_exclusive_bound=APIVersion(2, 13), high_inclusive_bound=APIVersion(2, 23)
    ),
)
def test_air_gap_over_trash_or_waste_chute_raises(
    decoy: Decoy,
    mock_instrument_core: InstrumentCore,
    mock_protocol_core: ProtocolCore,
    subject: InstrumentContext,
) -> None:
    """It should raise if a disposal location is the last accessed on versions below 2.23."""
    mock_chute = decoy.mock(cls=WasteChute)
    mock_trash = decoy.mock(cls=TrashBin)

    decoy.when(mock_instrument_core.has_tip()).then_return(True)
    decoy.when(mock_protocol_core.get_last_location()).then_return(mock_chute)

    with pytest.raises(RuntimeError, match="not valid for air gap"):
        subject.air_gap(volume=10, height=11)

    decoy.when(mock_protocol_core.get_last_location()).then_return(mock_trash)

    with pytest.raises(RuntimeError, match="not valid for air gap"):
        subject.air_gap(volume=10, height=11)


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_raises_for_invalid_locations(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if source or destination is invalid."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    with pytest.raises(ValueError):
        subject.transfer_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[],
            dest=[[mock_well]],
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_raises_for_unequal_source_and_dest(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if source and destination are not of same length."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    with pytest.raises(
        ValueError, match="Sources and destinations should be of the same length"
    ):
        subject.transfer_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=mock_well,
            dest=[mock_well, mock_well],
            trash_location=trash_location,
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_raises_for_non_liquid_handling_locations(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if source or dest are invalid for liquid handling."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(
        mock_instrument_support.validate_takes_liquid(
            mock_well.top(), reject_module=True, reject_adapter=True
        )
    ).then_raise(ValueError("Uh oh"))
    with pytest.raises(ValueError, match="Uh oh"):
        subject.transfer_with_liquid_class(
            liquid_class=test_liq_class, volume=10, source=[mock_well], dest=[mock_well]
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_raises_for_bad_tip_policy(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if new_tip is invalid."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    with pytest.raises(ValueError, match="invalid value for 'new_tip'"):
        subject.transfer_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well],
            dest=[mock_well],
            new_tip="twice",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_raises_for_no_tip(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if there is no tip attached."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    with pytest.raises(RuntimeError, match="Pipette has no tip"):
        subject.transfer_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well],
            dest=[mock_well],
            new_tip="never",
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_raises_if_tip_has_liquid(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if tip has liquid before starting transfer."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    tip_racks = [decoy.mock(cls=Labware)]

    subject.starting_tip = None
    subject.tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(
        labware.next_available_tip(
            starting_tip=None,
            tip_racks=tip_racks,
            channels=2,
            nozzle_map=MOCK_MAP,
        )
    ).then_return((decoy.mock(cls=Labware), decoy.mock(cls=Well)))
    decoy.when(mock_instrument_core.get_current_volume()).then_return(1000)
    with pytest.raises(RuntimeError, match="liquid already in the tip"):
        subject.transfer_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well],
            dest=[mock_well],
            new_tip="once",
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_delegates_to_engine_core(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should delegate the transfer execution to core."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    mock_starting_tip_well = decoy.mock(cls=Well)
    tip_racks = [decoy.mock(cls=Labware)]
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    next_tiprack = decoy.mock(cls=Labware)
    subject.starting_tip = mock_starting_tip_well
    subject._tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    decoy.when(next_tiprack.uri).then_return("tiprack-uri")
    decoy.when(mock_instrument_core.get_pipette_name()).then_return("pipette-name")
    subject.transfer_with_liquid_class(
        liquid_class=test_liq_class,
        volume=10,
        source=[mock_well],
        dest=[mock_well],
        new_tip="once",
        trash_location=trash_location,
        return_tip=True,
    )
    decoy.verify(
        mock_instrument_core.transfer_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[(Location(Point(), labware=mock_well), mock_well._core)],
            dest=[(Location(Point(), labware=mock_well), mock_well._core)],
            new_tip=TransferTipPolicyV2.ONCE,
            tip_racks=[(Location(Point(), labware=tip_racks[0]), tip_racks[0]._core)],
            starting_tip=mock_starting_tip_well._core,
            trash_location=trash_location,
            return_tip=True,
            keep_last_tip=False,
            last_tip_location=None,
        )
    )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_multi_channel_delegates_to_engine_core(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should delegate the transfer execution to core for a multi-channel pipette."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_well.well_name).then_return("mock well")
    tip_racks = [decoy.mock(cls=Labware)]
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    next_tiprack = decoy.mock(cls=Labware)
    subject.starting_tip = None
    subject._tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(2)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(
        mock_tx_liquid_utils.group_wells_for_multi_channel_transfer(
            [mock_well, mock_well], mock_nozzle_map, "source"
        )
    ).then_return([mock_well])
    decoy.when(
        mock_tx_liquid_utils.group_wells_for_multi_channel_transfer(
            [mock_well, mock_well], mock_nozzle_map, "destination"
        )
    ).then_return([mock_well])

    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    decoy.when(next_tiprack.uri).then_return("tiprack-uri")
    decoy.when(mock_instrument_core.get_pipette_name()).then_return("pipette-name")
    subject.transfer_with_liquid_class(
        liquid_class=test_liq_class,
        volume=10,
        source=[[mock_well, mock_well]],
        dest=[[mock_well, mock_well]],
        new_tip="once",
        trash_location=trash_location,
        return_tip=True,
    )
    decoy.verify(
        mock_instrument_core.transfer_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[(Location(Point(), labware=mock_well), mock_well._core)],
            dest=[(Location(Point(), labware=mock_well), mock_well._core)],
            new_tip=TransferTipPolicyV2.ONCE,
            tip_racks=[(Location(Point(), labware=tip_racks[0]), tip_racks[0]._core)],
            starting_tip=None,
            trash_location=trash_location,
            return_tip=True,
            keep_last_tip=False,
            last_tip_location=None,
        )
    )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_transfer_liquid_delegates_to_engine_core_with_trash_destination(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should delegate the transfer execution to core with a trash location as the destination."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    mock_starting_tip_well = decoy.mock(cls=Well)
    mock_trash = decoy.mock(cls=TrashBin)
    tip_racks = [decoy.mock(cls=Labware)]
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    next_tiprack = decoy.mock(cls=Labware)
    subject.starting_tip = mock_starting_tip_well
    subject._tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    decoy.when(next_tiprack.uri).then_return("tiprack-uri")
    decoy.when(mock_instrument_core.get_pipette_name()).then_return("pipette-name")
    subject.transfer_with_liquid_class(
        liquid_class=test_liq_class,
        volume=10,
        source=[mock_well],
        dest=mock_trash,
        new_tip="once",
        trash_location=trash_location,
        return_tip=True,
    )
    decoy.verify(
        mock_instrument_core.transfer_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[(Location(Point(), labware=mock_well), mock_well._core)],
            dest=mock_trash,
            new_tip=TransferTipPolicyV2.ONCE,
            tip_racks=[(Location(Point(), labware=tip_racks[0]), tip_racks[0]._core)],
            starting_tip=mock_starting_tip_well._core,
            trash_location=trash_location,
            return_tip=True,
            keep_last_tip=False,
            last_tip_location=None,
        )
    )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_distribute_liquid_raises_if_more_than_one_source(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise error if source is more than one well."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    with pytest.raises(ValueError, match="Source should be a single well"):
        subject.distribute_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well, mock_well],
            dest=[mock_well],
            trash_location=trash_location,
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_distribute_liquid_raises_for_non_liquid_handling_locations(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if source or dest are invalid for liquid handling."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(
        mock_instrument_support.validate_takes_liquid(
            mock_well.top(), reject_module=True, reject_adapter=True
        )
    ).then_raise(ValueError("Uh oh"))
    with pytest.raises(ValueError, match="Uh oh"):
        subject.distribute_with_liquid_class(
            liquid_class=test_liq_class, volume=10, source=mock_well, dest=[mock_well]
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_distribute_liquid_raises_for_bad_tip_policy(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if new_tip is invalid."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    with pytest.raises(ValueError, match="invalid value for 'new_tip'"):
        subject.distribute_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=mock_well,
            dest=[mock_well],
            new_tip="twice",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_distribute_liquid_raises_for_no_tip(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if there is no tip attached."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    with pytest.raises(RuntimeError, match="Pipette has no tip"):
        subject.distribute_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=mock_well,
            dest=[mock_well],
            new_tip="never",
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_distribute_liquid_raises_if_tip_has_liquid(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if the tip has liquid at the start of distribution."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    tip_racks = [decoy.mock(cls=Labware)]

    subject.starting_tip = None
    subject.tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(
        labware.next_available_tip(
            starting_tip=None,
            tip_racks=tip_racks,
            channels=2,
            nozzle_map=MOCK_MAP,
        )
    ).then_return((decoy.mock(cls=Labware), decoy.mock(cls=Well)))
    decoy.when(mock_instrument_core.get_current_volume()).then_return(1000)
    with pytest.raises(RuntimeError, match="liquid already in the tip"):
        subject.distribute_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=mock_well,
            dest=[mock_well],
            new_tip="once",
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
@pytest.mark.parametrize("new_tip", ["per source", "per destination"])
def test_distribute_liquid_raises_for_incompatible_tip_policies(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    new_tip: TransferTipPolicyV2Type,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if the tip policy is "per source" or "per destination"."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    with pytest.raises(ValueError, match="Incompatible `new_tip` value"):
        subject.distribute_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=mock_well,
            dest=[mock_well],
            new_tip=new_tip,
            trash_location=trash_location,
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_distribute_liquid_delegates_to_engine_core(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should delegate the distribute execution to core."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    mock_starting_tip_well = decoy.mock(cls=Well)
    tip_racks = [decoy.mock(cls=Labware)]
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    next_tiprack = decoy.mock(cls=Labware)
    subject.starting_tip = mock_starting_tip_well
    subject._tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    decoy.when(next_tiprack.uri).then_return("tiprack-uri")
    decoy.when(mock_instrument_core.get_pipette_name()).then_return("pipette-name")
    subject.distribute_with_liquid_class(
        liquid_class=test_liq_class,
        volume=10,
        source=mock_well,
        dest=[mock_well],
        new_tip="once",
        trash_location=trash_location,
        return_tip=True,
    )
    decoy.verify(
        mock_instrument_core.distribute_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=(Location(Point(), labware=mock_well), mock_well._core),
            dest=[(Location(Point(), labware=mock_well), mock_well._core)],
            new_tip=TransferTipPolicyV2.ONCE,
            tip_racks=[(Location(Point(), labware=tip_racks[0]), tip_racks[0]._core)],
            starting_tip=mock_starting_tip_well._core,
            trash_location=trash_location,
            return_tip=True,
            keep_last_tip=False,
            last_tip_location=None,
        )
    )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_distribute_liquid_multi_channel_delegates_to_engine_core(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should delegate the distribute execution to core for a multi-channel pipette."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_well.well_name).then_return("mock well")
    tip_racks = [decoy.mock(cls=Labware)]
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    next_tiprack = decoy.mock(cls=Labware)
    subject.starting_tip = None
    subject._tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(2)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(
        mock_tx_liquid_utils.group_wells_for_multi_channel_transfer(
            [mock_well, mock_well, mock_well], mock_nozzle_map, "source"
        )
    ).then_return([mock_well])
    decoy.when(
        mock_tx_liquid_utils.group_wells_for_multi_channel_transfer(
            [mock_well, mock_well], mock_nozzle_map, "destination"
        )
    ).then_return([mock_well, mock_well])

    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    decoy.when(next_tiprack.uri).then_return("tiprack-uri")
    decoy.when(mock_instrument_core.get_pipette_name()).then_return("pipette-name")
    subject.distribute_with_liquid_class(
        liquid_class=test_liq_class,
        volume=10,
        source=[mock_well, mock_well, mock_well],
        dest=[[mock_well, mock_well]],
        new_tip="once",
        trash_location=trash_location,
        return_tip=True,
    )
    decoy.verify(
        mock_instrument_core.distribute_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=(Location(Point(), labware=mock_well), mock_well._core),
            dest=[
                (Location(Point(), labware=mock_well), mock_well._core),
                (Location(Point(), labware=mock_well), mock_well._core),
            ],
            new_tip=TransferTipPolicyV2.ONCE,
            tip_racks=[(Location(Point(), labware=tip_racks[0]), tip_racks[0]._core)],
            starting_tip=None,
            trash_location=trash_location,
            return_tip=True,
            keep_last_tip=False,
            last_tip_location=None,
        )
    )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_consolidate_liquid_raises_if_more_than_one_destination(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise error if destination is more than one well."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    with pytest.raises(ValueError, match="Destination should be a single well"):
        subject.consolidate_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well, mock_well],
            dest=[mock_well, mock_well],
            trash_location=trash_location,
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_consolidate_liquid_raises_for_non_liquid_handling_locations(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if sources or destination are not a valid liquid handling target."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(
        mock_instrument_support.validate_takes_liquid(
            mock_well.top(), reject_module=True, reject_adapter=True
        )
    ).then_raise(ValueError("Uh oh"))
    with pytest.raises(ValueError, match="Uh oh"):
        subject.consolidate_with_liquid_class(
            liquid_class=test_liq_class, volume=10, source=[mock_well], dest=mock_well
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_consolidate_liquid_raises_for_bad_tip_policy(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if new_tip is invalid."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    with pytest.raises(ValueError, match="invalid value for 'new_tip'"):
        subject.consolidate_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well],
            dest=mock_well,
            new_tip="whenever",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_consolidate_liquid_raises_for_no_tip(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if there is no tip attached."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    with pytest.raises(RuntimeError, match="Pipette has no tip"):
        subject.consolidate_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well],
            dest=mock_well,
            new_tip="never",
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_consolidate_liquid_raises_if_tip_has_liquid(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if there is liquid in the tip."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    tip_racks = [decoy.mock(cls=Labware)]

    subject.starting_tip = None
    subject.tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(1000)
    with pytest.raises(RuntimeError, match="liquid already in the tip"):
        subject.consolidate_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well],
            dest=mock_well,
            new_tip="once",
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
@pytest.mark.parametrize("new_tip", ["per source", "per destination"])
def test_consolidate_liquid_raises_for_incompatible_tip_policies(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    new_tip: TransferTipPolicyV2Type,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should raise errors if the tip policy is "per source" or "per destination"."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(1)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    with pytest.raises(ValueError, match="Incompatible `new_tip` value."):
        subject.consolidate_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[mock_well],
            dest=mock_well,
            new_tip=new_tip,
            trash_location=trash_location,
        )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_consolidate_liquid_delegates_to_engine_core(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should delegate the consolidate execution to core."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    mock_starting_tip_well = decoy.mock(cls=Well)
    tip_racks = [decoy.mock(cls=Labware)]
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    next_tiprack = decoy.mock(cls=Labware)
    subject.starting_tip = mock_starting_tip_well
    subject._tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    decoy.when(next_tiprack.uri).then_return("tiprack-uri")
    decoy.when(mock_instrument_core.get_pipette_name()).then_return("pipette-name")

    subject.consolidate_with_liquid_class(
        liquid_class=test_liq_class,
        volume=10,
        source=[mock_well],
        dest=mock_well,
        new_tip="once",
        trash_location=trash_location,
        return_tip=True,
    )
    decoy.verify(
        mock_instrument_core.consolidate_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[(Location(Point(), labware=mock_well), mock_well._core)],
            dest=(Location(Point(), labware=mock_well), mock_well._core),
            new_tip=TransferTipPolicyV2.ONCE,
            tip_racks=[(Location(Point(), labware=tip_racks[0]), tip_racks[0]._core)],
            starting_tip=mock_starting_tip_well._core,
            trash_location=trash_location,
            return_tip=True,
            keep_last_tip=False,
            last_tip_location=None,
        )
    )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_consolidate_liquid_multi_channel_delegates_to_engine_core(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should delegate the consolidate execution to core for a multi-channel pipette."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    decoy.when(mock_well.well_name).then_return("mock well")
    tip_racks = [decoy.mock(cls=Labware)]
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    next_tiprack = decoy.mock(cls=Labware)
    subject.starting_tip = None
    subject._tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    mock_nozzle_map = decoy.mock(cls=NozzleMapInterface)
    decoy.when(mock_nozzle_map.tip_count).then_return(2)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(mock_nozzle_map)
    decoy.when(
        mock_tx_liquid_utils.group_wells_for_multi_channel_transfer(
            [mock_well, mock_well], mock_nozzle_map, "source"
        )
    ).then_return([mock_well, mock_well])
    decoy.when(
        mock_tx_liquid_utils.group_wells_for_multi_channel_transfer(
            [mock_well, mock_well, mock_well], mock_nozzle_map, "destination"
        )
    ).then_return([mock_well])

    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    decoy.when(next_tiprack.uri).then_return("tiprack-uri")
    decoy.when(mock_instrument_core.get_pipette_name()).then_return("pipette-name")

    subject.consolidate_with_liquid_class(
        liquid_class=test_liq_class,
        volume=10,
        source=[[mock_well, mock_well]],
        dest=[mock_well, mock_well, mock_well],
        new_tip="once",
        trash_location=trash_location,
        return_tip=True,
    )
    decoy.verify(
        mock_instrument_core.consolidate_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[
                (Location(Point(), labware=mock_well), mock_well._core),
                (Location(Point(), labware=mock_well), mock_well._core),
            ],
            dest=(Location(Point(), labware=mock_well), mock_well._core),
            new_tip=TransferTipPolicyV2.ONCE,
            tip_racks=[(Location(Point(), labware=tip_racks[0]), tip_racks[0]._core)],
            starting_tip=None,
            trash_location=trash_location,
            return_tip=True,
            keep_last_tip=False,
            last_tip_location=None,
        )
    )


@pytest.mark.parametrize("robot_type", ["OT-2 Standard", "OT-3 Standard"])
def test_consolidate_liquid_delegates_to_engine_core_with_trash_destination(
    decoy: Decoy,
    mock_protocol_core: ProtocolCore,
    mock_instrument_core: InstrumentCore,
    subject: InstrumentContext,
    robot_type: RobotType,
    minimal_liquid_class_def2: LiquidClassSchemaV1,
) -> None:
    """It should delegate the consolidate execution to core."""
    test_liq_class = LiquidClass.create(minimal_liquid_class_def2)
    mock_well = decoy.mock(cls=Well)
    mock_starting_tip_well = decoy.mock(cls=Well)
    mock_waste_chute = decoy.mock(cls=WasteChute)
    tip_racks = [decoy.mock(cls=Labware)]
    trash_location = Location(point=Point(1, 2, 3), labware=mock_well)
    next_tiprack = decoy.mock(cls=Labware)
    subject.starting_tip = mock_starting_tip_well
    subject._tip_racks = tip_racks

    decoy.when(mock_protocol_core.robot_type).then_return(robot_type)
    decoy.when(mock_instrument_core.get_nozzle_map()).then_return(MOCK_MAP)
    decoy.when(mock_instrument_core.get_active_channels()).then_return(2)
    decoy.when(mock_instrument_core.get_current_volume()).then_return(0)
    decoy.when(next_tiprack.uri).then_return("tiprack-uri")
    decoy.when(mock_instrument_core.get_pipette_name()).then_return("pipette-name")

    subject.consolidate_with_liquid_class(
        liquid_class=test_liq_class,
        volume=10,
        source=[mock_well],
        dest=mock_waste_chute,
        new_tip="once",
        trash_location=trash_location,
        return_tip=True,
    )
    decoy.verify(
        mock_instrument_core.consolidate_with_liquid_class(
            liquid_class=test_liq_class,
            volume=10,
            source=[(Location(Point(), labware=mock_well), mock_well._core)],
            dest=mock_waste_chute,
            new_tip=TransferTipPolicyV2.ONCE,
            tip_racks=[(Location(Point(), labware=tip_racks[0]), tip_racks[0]._core)],
            starting_tip=mock_starting_tip_well._core,
            trash_location=trash_location,
            return_tip=True,
            keep_last_tip=False,
            last_tip_location=None,
        )
    )
