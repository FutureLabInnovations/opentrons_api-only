"""Test evotip dispense in place commands."""

import pytest
from decoy import Decoy

from opentrons.protocol_engine import (
    LiquidHandlingWellLocation,
    WellOrigin,
    WellOffset,
    DeckPoint,
)
from opentrons.types import Point
from opentrons.protocol_engine.execution import (
    PipettingHandler,
    GantryMover,
    MovementHandler,
)

from opentrons_shared_data.labware.labware_definition import (
    LabwareDefinition,
    labware_definition_type_adapter,
)
from opentrons.protocol_engine.commands.command import SuccessData
from opentrons.protocol_engine.commands.pressure_dispense import (
    PressureDispenseParams,
    PressureDispenseResult,
    PressureDispenseImplementation,
)
from opentrons.protocol_engine.resources import ModelUtils
from opentrons.protocol_engine.state.state import StateView
from opentrons.protocol_engine.state import update_types

from opentrons_shared_data.labware import load_definition


@pytest.fixture
def evotips_definition() -> LabwareDefinition:
    """A fixturee of the evotips definition."""
    # TODO (chb 2025-01-29): When we migrate all labware to v3 we can clean this up
    return labware_definition_type_adapter.validate_python(
        load_definition("ev_resin_tips_flex_96_labware", 1)
    )


@pytest.fixture
def subject(
    pipetting: PipettingHandler,
    state_view: StateView,
    gantry_mover: GantryMover,
    model_utils: ModelUtils,
    movement: MovementHandler,
    **kwargs: object,
) -> PressureDispenseImplementation:
    """Build a command implementation."""
    return PressureDispenseImplementation(
        pipetting=pipetting,
        state_view=state_view,
        gantry_mover=gantry_mover,
        model_utils=model_utils,
        movement=movement,
    )


async def test_pressure_dispense_implementation(
    decoy: Decoy,
    movement: MovementHandler,
    gantry_mover: GantryMover,
    pipetting: PipettingHandler,
    state_view: StateView,
    subject: PressureDispenseImplementation,
    evotips_definition: LabwareDefinition,
) -> None:
    """It should dispense in place."""
    well_location = LiquidHandlingWellLocation(
        origin=WellOrigin.TOP, offset=WellOffset(x=0, y=0, z=0)
    )

    data = PressureDispenseParams(
        pipetteId="pipette-id-abc123",
        labwareId="labware-id-abc123",
        wellName="A3",
        volume=100,
        flowRate=456,
    )

    decoy.when(
        await movement.move_to_well(
            pipette_id="pipette-id-abc123",
            labware_id="labware-id-abc123",
            well_name="A3",
            well_location=well_location,
            current_well=None,
            force_direct=False,
            minimum_z_height=None,
            speed=None,
            operation_volume=None,
        )
    ).then_return(Point(x=1, y=2, z=3))

    decoy.when(state_view.labware.get_definition("labware-id-abc123")).then_return(
        evotips_definition
    )
    decoy.when(
        state_view.pipettes.get_aspirated_volume("pipette-id-abc123")
    ).then_return(100)

    decoy.when(pipetting.get_state_view()).then_return(state_view)

    decoy.when(
        await pipetting.dispense_in_place(
            pipette_id="pipette-id-abc123",
            volume=100.0,
            flow_rate=456.0,
            push_out=None,
            is_full_dispense=True,
            correction_volume=0,
        )
    ).then_return(100)

    decoy.when(await gantry_mover.get_position("pipette-id-abc123")).then_return(
        Point(1, 2, 3)
    )

    result = await subject.execute(data)

    assert result == SuccessData(
        public=PressureDispenseResult(volume=100),
        state_update=update_types.StateUpdate(
            pipette_location=update_types.PipetteLocationUpdate(
                pipette_id="pipette-id-abc123",
                new_location=update_types.Well(
                    labware_id="labware-id-abc123",
                    well_name="A3",
                ),
                new_deck_point=DeckPoint.model_construct(x=1, y=2, z=3),
            ),
            pipette_aspirated_fluid=update_types.PipetteEjectedFluidUpdate(
                pipette_id="pipette-id-abc123", volume=100
            ),
            ready_to_aspirate=update_types.PipetteAspirateReadyUpdate(
                pipette_id="pipette-id-abc123", ready_to_aspirate=False
            ),
        ),
    )
    decoy.verify(await pipetting.increase_evo_disp_count("pipette-id-abc123"), times=1)
