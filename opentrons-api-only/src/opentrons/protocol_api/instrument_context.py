from __future__ import annotations
import logging
from contextlib import ExitStack
from typing import Any, List, Optional, Sequence, Union, cast, Tuple
from opentrons_shared_data.errors.exceptions import (
    CommandPreconditionViolated,
    CommandParameterLimitViolated,
    UnexpectedTipRemovalError,
    UnsupportedHardwareCommand,
)

from opentrons.legacy_broker import LegacyBroker
from opentrons.hardware_control.dev_types import PipetteDict
from opentrons import types
from opentrons.legacy_commands import (
    commands as cmds,
    protocol_commands as protocol_cmds,
)

from opentrons.legacy_commands import publisher
from opentrons.protocols.advanced_control.mix import mix_from_kwargs
from opentrons.protocols.advanced_control.transfers import transfer as v1_transfer
from opentrons.protocols.api_support.deck_type import NoTrashDefinedError
from opentrons.protocols.api_support.types import APIVersion
from opentrons.protocols.api_support import instrument
from opentrons.protocols.api_support.util import (
    FlowRates,
    PlungerSpeeds,
    clamp_value,
    requires_version,
    APIVersionError,
    UnsupportedAPIError,
)

from .core.common import InstrumentCore, ProtocolCore, WellCore
from .core.engine import ENGINE_CORE_API_VERSION
from .core.legacy.legacy_instrument_core import LegacyInstrumentCore
from .config import Clearances
from .disposal_locations import TrashBin, WasteChute
from ._nozzle_layout import NozzleLayout
from ._liquid import LiquidClass
from ._transfer_liquid_validation import (
    verify_and_normalize_transfer_args,
    resolve_keep_last_tip,
)
from . import labware, validation
from ..protocols.advanced_control.transfers.common import (
    TransferTipPolicyV2,
    TransferTipPolicyV2Type,
)
from ..protocol_engine.types import LiquidTrackingType

_DEFAULT_ASPIRATE_CLEARANCE = 1.0
_DEFAULT_DISPENSE_CLEARANCE = 1.0

_log = logging.getLogger(__name__)

_PREP_AFTER_ADDED_IN = APIVersion(2, 13)
"""The version after which the pick-up tip procedure should also prepare the plunger."""
_PRESSES_INCREMENT_REMOVED_IN = APIVersion(2, 14)
"""The version after which the pick-up tip procedure deprecates presses and increment arguments."""
_DROP_TIP_LOCATION_ALTERNATING_ADDED_IN = APIVersion(2, 15)
"""The version after which a drop-tip-into-trash procedure drops tips in different alternating locations within the trash well."""
_PARTIAL_NOZZLE_CONFIGURATION_ADDED_IN = APIVersion(2, 16)
"""The version after which a partial nozzle configuration became available for the 96 Channel Pipette."""
_PARTIAL_NOZZLE_CONFIGURATION_AUTOMATIC_TIP_TRACKING_IN = APIVersion(2, 18)
"""The version after which automatic tip tracking supported partially configured nozzle layouts."""
_DISPOSAL_LOCATION_OFFSET_ADDED_IN = APIVersion(2, 18)
"""The version after which offsets for deck configured trash containers and changes to alternating tip drop behavior were introduced."""
_PARTIAL_NOZZLE_CONFIGURATION_SINGLE_ROW_PARTIAL_COLUMN_ADDED_IN = APIVersion(2, 20)
"""The version after which partial nozzle configurations of single, row, and partial column layouts became available."""
_AIR_GAP_TRACKING_ADDED_IN = APIVersion(2, 22)
"""The version after which air gaps should be implemented with a separate call instead of an aspirate for better liquid volume tracking."""


AdvancedLiquidHandling = v1_transfer.AdvancedLiquidHandling


class _Unset:
    """A sentinel value when no value has been supplied for an argument.
    User code should never use this explicitly."""

    def __repr__(self) -> str:
        # Without this, the generated docs render the argument as
        # "<opentrons.protocol_api.instrument_context._Unset object at 0x1234>"
        return self.__class__.__name__


class InstrumentContext(publisher.CommandPublisher):
    """
    A context for a specific pipette or instrument.

    The InstrumentContext class provides the objects, attributes, and methods that allow
    you to use pipettes in your protocols.

    Methods generally fall into one of two categories.

      - They can change the state of the InstrumentContext object, like how fast it
        moves liquid or where it disposes of used tips.

      - They can command the instrument to perform an action, like picking up tips,
        moving to certain locations, and aspirating or dispensing liquid.

    Objects in this class should not be instantiated directly. Instead, instances are
    returned by :py:meth:`ProtocolContext.load_instrument`.

    .. versionadded:: 2.0

    """

    def __init__(
        self,
        core: InstrumentCore,
        protocol_core: ProtocolCore,
        broker: LegacyBroker,
        api_version: APIVersion,
        tip_racks: List[labware.Labware],
        trash: Optional[Union[labware.Labware, TrashBin, WasteChute]],
        requested_as: str,
    ) -> None:
        super().__init__(broker)
        self._api_version = api_version
        self._core = core
        self._protocol_core = protocol_core
        self._tip_racks = tip_racks
        self._last_tip_picked_up_from: Union[labware.Well, None] = None
        self._starting_tip: Union[labware.Well, None] = None
        self._well_bottom_clearances = Clearances(
            default_aspirate=_DEFAULT_ASPIRATE_CLEARANCE,
            default_dispense=_DEFAULT_DISPENSE_CLEARANCE,
        )
        self._user_specified_trash: Union[
            labware.Labware, TrashBin, WasteChute, None
        ] = trash
        self.requested_as = requested_as

    @property
    @requires_version(2, 0)
    def api_version(self) -> APIVersion:
        return self._api_version

    @property
    @requires_version(2, 0)
    def starting_tip(self) -> Union[labware.Well, None]:
        """
        Which well of a tip rack the pipette should start at when automatically choosing tips to pick up.

        See :py:meth:`.pick_up_tip()`.

        .. note::

            In robot software versions 6.3.0 and 6.3.1, protocols specifying API level
            2.14 ignored ``starting_tip`` on the second and subsequent calls to
            :py:meth:`.InstrumentContext.pick_up_tip` with no argument. This is fixed
            for all API levels as of robot software version 7.0.0.
        """
        return self._starting_tip

    @starting_tip.setter
    def starting_tip(self, location: Union[labware.Well, None]) -> None:
        self._starting_tip = location

    @requires_version(2, 0)
    def reset_tipracks(self) -> None:
        """Reload all tips in each tip rack and reset the starting tip."""
        for tiprack in self.tip_racks:
            tiprack.reset()
        self.starting_tip = None

    @property
    @requires_version(2, 0)
    def default_speed(self) -> float:
        """The speed at which the robot's gantry moves in mm/s.

        The default speed for Flex varies between 300 and 350 mm/s. The OT-2 default is
        400 mm/s. In addition to changing the default, the speed of individual motions
        can be changed with the ``speed`` argument of the
        :py:meth:`InstrumentContext.move_to` method. See :ref:`gantry_speed`.
        """
        return self._core.get_default_speed()

    @default_speed.setter
    def default_speed(self, speed: float) -> None:
        self._core.set_default_speed(speed)

    @requires_version(2, 21)
    def get_minimum_liquid_sense_height(self) -> float:
        """Get the minimum allowed height for liquid-level detection."""
        return self._core.get_minimum_liquid_sense_height()

    @requires_version(2, 0)
    def aspirate(  # noqa: C901
        self,
        volume: Optional[float] = None,
        location: Optional[Union[types.Location, labware.Well]] = None,
        rate: float = 1.0,
        flow_rate: Optional[float] = None,
    ) -> InstrumentContext:
        """
        Draw liquid into a pipette tip.

        See :ref:`new-aspirate` for more details and examples.

        :param volume: The volume to aspirate, measured in µL. If unspecified,
                    defaults to the maximum volume for the pipette and its currently
                    attached tip.

                    If ``aspirate`` is called with a volume of precisely 0, its behavior
                    depends on the API level of the protocol. On API levels below 2.16,
                    it will behave the same as a volume of ``None``/unspecified: aspirate
                    until the pipette is full. On API levels at or above 2.16, no liquid
                    will be aspirated.
        :type volume: int or float
        :param location: Tells the robot where to aspirate from. The location can be
                         a :py:class:`.Well` or a :py:class:`.Location`.

                            - If the location is a ``Well``, the robot will aspirate at
                              or above the bottom center of the well. The distance (in mm)
                              from the well bottom is specified by
                              :py:obj:`well_bottom_clearance.aspirate
                              <well_bottom_clearance>`.

                            - If the location is a ``Location`` (e.g., the result of
                              :py:meth:`.Well.top` or :py:meth:`.Well.bottom`), the robot
                              will aspirate from that specified position.

                            - If the ``location`` is unspecified, the robot will
                              aspirate from its current position.
        :param rate: A multiplier for the default flow rate of the pipette. Calculated
                     as ``rate`` multiplied by :py:attr:`flow_rate.aspirate
                     <flow_rate>`. If not specified, defaults to 1.0. See
                     :ref:`new-plunger-flow-rates`.
        :type rate: float
        :param flow_rate: The absolute flow rate in µL/s. If ``flow_rate`` is specified,
                          ``rate`` must not be set.
        :type flow_rate: float
        :returns: This instance.

        .. note::

            If ``aspirate`` is called with a single, unnamed argument, it will treat
            that argument as ``volume``. If you want to call ``aspirate`` with only
            ``location``, specify it as a keyword argument:
            ``pipette.aspirate(location=plate['A1'])``

        .. versionchanged:: 2.24
            Added the ``flow_rate`` parameter.
        """
        if flow_rate is not None:
            if self.api_version < APIVersion(2, 24):
                raise APIVersionError(
                    api_element="flow_rate",
                    until_version="2.24",
                    current_version=f"{self.api_version}",
                )
            if rate != 1.0:
                raise ValueError("rate must not be set if flow_rate is specified")
            rate = flow_rate / self._core.get_aspirate_flow_rate()
        else:
            flow_rate = self._core.get_aspirate_flow_rate(rate)

        _log.debug(
            "aspirate {} from {} at {} µL/s".format(
                volume, location if location else "current position", flow_rate
            )
        )

        move_to_location: types.Location
        well: Optional[labware.Well]
        last_location = self._get_last_location_by_api_version()
        try:
            target = validation.validate_location(
                location=location, last_location=last_location
            )
        except validation.NoLocationError as e:
            raise RuntimeError(
                "If aspirate is called without an explicit location, another"
                " method that moves to a location (such as move_to or "
                "dispense) must previously have been called so the robot "
                "knows where it is."
            ) from e

        if isinstance(target, validation.DisposalTarget):
            raise ValueError(
                "Trash Bin and Waste Chute are not acceptable location parameters for Aspirate commands."
            )
        move_to_location, well, meniscus_tracking = self._handle_aspirate_target(
            target=target
        )
        if self.api_version >= APIVersion(2, 11):
            instrument.validate_takes_liquid(
                location=move_to_location,
                reject_module=self.api_version >= APIVersion(2, 13),
                reject_adapter=self.api_version >= APIVersion(2, 15),
            )

        if self.api_version >= APIVersion(2, 16):
            c_vol = self._core.get_available_volume() if volume is None else volume
        else:
            c_vol = self._core.get_available_volume() if not volume else volume

        if (
            self.api_version >= APIVersion(2, 20)
            and well is not None
            and self.liquid_presence_detection
            and self._core.nozzle_configuration_valid_for_lld()
            and self._core.get_current_volume() == 0
            and self._core.get_has_clean_tip()
        ):
            self._raise_if_pressure_not_supported_by_pipette()
            self.require_liquid_presence(well=well)

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.aspirate(
                instrument=self,
                volume=c_vol,
                location=move_to_location,
                flow_rate=flow_rate,
                rate=rate,
            ),
        ):
            self._core.aspirate(
                location=move_to_location,
                well_core=well._core if well is not None else None,
                volume=c_vol,
                rate=rate,
                flow_rate=flow_rate,
                in_place=target.in_place,
                meniscus_tracking=meniscus_tracking,
            )

        return self

    @requires_version(2, 0)
    def dispense(  # noqa: C901
        self,
        volume: Optional[float] = None,
        location: Optional[
            Union[types.Location, labware.Well, TrashBin, WasteChute]
        ] = None,
        rate: float = 1.0,
        push_out: Optional[float] = None,
        flow_rate: Optional[float] = None,
    ) -> InstrumentContext:
        """
        Dispense liquid from a pipette tip.

        See :ref:`new-dispense` for more details and examples.

        :param volume: The volume to dispense, measured in µL.

                         - If unspecified or ``None``, dispense the :py:attr:`current_volume`.

                         - If 0, the behavior of ``dispense()`` depends on the API level
                           of the protocol. In API version 2.16 and earlier, dispense all
                           liquid in the pipette (same as unspecified or ``None``). In API
                           version 2.17 and later, dispense no liquid.

                         - If greater than :py:obj:`.current_volume`, the behavior of
                           ``dispense()`` depends on the API level of the protocol. In API
                           version 2.16 and earlier, dispense all liquid in the pipette.
                           In API version 2.17 and later, raise an error.

        :type volume: int or float

        :param location: Tells the robot where to dispense liquid held in the pipette.
            The location can be a :py:class:`.Well`, :py:class:`.Location`,
            :py:class:`.TrashBin`, or :py:class:`.WasteChute`.

                            - If a ``Well``, the pipette will dispense
                              at or above the bottom center of the well. The distance (in
                              mm) from the well bottom is specified by
                              :py:obj:`well_bottom_clearance.dispense
                              <well_bottom_clearance>`.

                            - If a ``Location`` (e.g., the result of
                              :py:meth:`.Well.top` or :py:meth:`.Well.bottom`), the pipette
                              will dispense at that specified position.

                            - If a trash container, the pipette will dispense at a location
                              relative to its center and the trash container's top center.
                              See :ref:`position-relative-trash` for details.

                            - If unspecified, the pipette will
                              dispense at its current position.

                            If only a ``location`` is passed (e.g.,
                            ``pipette.dispense(location=plate['A1'])``), all of the
                            liquid aspirated into the pipette will be dispensed (the
                            amount is accessible through :py:attr:`current_volume`).

            .. versionchanged:: 2.16
                Accepts ``TrashBin`` and ``WasteChute`` values.

        :param rate: How quickly a pipette dispenses liquid. The speed in µL/s is
                     calculated as ``rate`` multiplied by :py:attr:`flow_rate.dispense
                     <flow_rate>`. If not specified, defaults to 1.0. See
                     :ref:`new-plunger-flow-rates`.
        :type rate: float

        :param push_out: Continue past the plunger bottom to help ensure all liquid
                         leaves the tip. Measured in µL. The default value is ``None``.

                         When not specified or set to ``None``, the plunger moves by a non-zero default amount.

                         For a table of default values, see :ref:`push-out-dispense`.
        :type push_out: float

        :param flow_rate: The absolute flow rate in µL/s. If ``flow_rate`` is specified,
                          ``rate`` must not be set.
        :type flow_rate: float

        :returns: This instance.

        .. note::

            If ``dispense`` is called with a single, unnamed argument, it will treat
            that argument as ``volume``. If you want to call ``dispense`` with only
            ``location``, specify it as a keyword argument:
            ``pipette.dispense(location=plate['A1'])``.

        .. versionchanged:: 2.15
            Added the ``push_out`` parameter.

        .. versionchanged:: 2.17
            Behavior of the ``volume`` parameter.

        .. versionchanged:: 2.24
            Added the ``flow_rate`` parameter.

        .. versionchanged:: 2.24
            ``location`` is no longer required if the pipette just moved to, dispensed, or blew out
            into a trash bin or waste chute.
        """
        if self.api_version < APIVersion(2, 15) and push_out:
            raise APIVersionError(
                api_element="Parameter push_out",
                until_version="2.15",
                current_version=f"{self.api_version}",
            )

        if flow_rate is not None:
            if self.api_version < APIVersion(2, 24):
                raise APIVersionError(
                    api_element="flow_rate",
                    until_version="2.24",
                    current_version=f"{self.api_version}",
                )
            if rate != 1.0:
                raise ValueError("rate must not be set if flow_rate is specified")
            rate = flow_rate / self._core.get_dispense_flow_rate()
        else:
            flow_rate = self._core.get_dispense_flow_rate(rate)

        _log.debug(
            "dispense {} from {} at {} µL/s".format(
                volume, location if location else "current position", flow_rate
            )
        )

        last_location = self._get_last_location_by_api_version()
        try:
            target = validation.validate_location(
                location=location, last_location=last_location
            )
        except validation.NoLocationError as e:
            raise RuntimeError(
                "If dispense is called without an explicit location, another"
                " method that moves to a location (such as move_to or "
                "aspirate) must previously have been called so the robot "
                "knows where it is."
            ) from e

        if self.api_version >= APIVersion(2, 16):
            c_vol = self._core.get_current_volume() if volume is None else volume
        else:
            c_vol = self._core.get_current_volume() if not volume else volume

        if isinstance(target, validation.DisposalTarget):
            with publisher.publish_context(
                broker=self.broker,
                command=cmds.dispense_in_disposal_location(
                    instrument=self,
                    volume=c_vol,
                    location=target.location,
                    rate=rate,
                    flow_rate=flow_rate,
                ),
            ):
                self._core.dispense(
                    volume=c_vol,
                    rate=rate,
                    location=target.location,
                    well_core=None,
                    flow_rate=flow_rate,
                    in_place=target.in_place,
                    push_out=push_out,
                    meniscus_tracking=None,
                )
            return self

        move_to_location, well, meniscus_tracking = self._handle_dispense_target(
            target=target
        )

        if self.api_version >= APIVersion(2, 11):
            instrument.validate_takes_liquid(
                location=move_to_location,
                reject_module=self.api_version >= APIVersion(2, 13),
                reject_adapter=self.api_version >= APIVersion(2, 15),
            )

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.dispense(
                instrument=self,
                volume=c_vol,
                location=move_to_location,
                rate=rate,
                flow_rate=flow_rate,
            ),
        ):
            self._core.dispense(
                volume=c_vol,
                rate=rate,
                location=move_to_location,
                well_core=well._core if well is not None else None,
                flow_rate=flow_rate,
                in_place=target.in_place,
                push_out=push_out,
                meniscus_tracking=meniscus_tracking,
            )

        return self

    @requires_version(2, 0)
    def mix(  # noqa: C901
        self,
        repetitions: int = 1,
        volume: Optional[float] = None,
        location: Optional[Union[types.Location, labware.Well]] = None,
        rate: float = 1.0,
        aspirate_flow_rate: Optional[float] = None,
        dispense_flow_rate: Optional[float] = None,
        aspirate_delay: Optional[float] = None,
        dispense_delay: Optional[float] = None,
        final_push_out: Optional[float] = None,
    ) -> InstrumentContext:
        """
        Mix a volume of liquid by repeatedly aspirating and dispensing it in a single location.

        See :ref:`mix` for examples.

        :param repetitions: Number of times to mix (default is 1).
        :param volume: The volume to mix, measured in µL. If unspecified, defaults
                       to the maximum volume for the pipette and its attached tip.

                       If ``mix`` is called with a volume of precisely 0, its behavior
                       depends on the API level of the protocol. On API levels below 2.16,
                       it will behave the same as a volume of ``None``/unspecified: mix
                       the full working volume of the pipette. On API levels at or above 2.16,
                       no liquid will be mixed.
        :param location: The :py:class:`.Well` or :py:class:`~.types.Location` where the
                        pipette will mix. If unspecified, the pipette will mix at its
                        current position.
        :param rate: How quickly the pipette aspirates and dispenses liquid while
                     mixing. The aspiration flow rate is calculated as ``rate``
                     multiplied by :py:attr:`flow_rate.aspirate <flow_rate>`. The
                     dispensing flow rate is calculated as ``rate`` multiplied by
                     :py:attr:`flow_rate.dispense <flow_rate>`. See
                     :ref:`new-plunger-flow-rates`.
        :param aspirate_flow_rate: The absolute flow rate for each aspirate in the mix, in µL/s.
                                   If this is specified, ``rate`` must not be set.
        :param dispense_flow_rate: The absolute flow rate for each dispense in the mix, in µL/s.
                                   If this is specified, ``rate`` must not be set.
        :param aspirate_delay: How long to wait after each aspirate in the mix, in seconds.
        :param dispense_delay: How long to wait after each dispense in the mix, in seconds.
        :param final_push_out: How much volume to push out after the final mix repetition. The
                               pipette will not push out after earlier repetitions. If
                               not specified or ``None``, the pipette will push out the
                               default non-zero amount. See :ref:`push-out-dispense`.
        :raises: ``UnexpectedTipRemovalError`` -- If no tip is attached to the pipette.
        :returns: This instance.

        .. note::

            All the arguments of ``mix`` are optional. However, if you omit one of them,
            all subsequent arguments must be passed as keyword arguments. For instance,
            ``pipette.mix(1, location=wellplate['A1'])`` is a valid call, but
            ``pipette.mix(1, wellplate['A1'])`` is not.

        .. versionchanged:: 2.21
            Does not repeatedly check for liquid presence.
        .. versionchanged:: 2.24
            Adds the ``aspirate_flow_rate``, ``dispense_flow_rate``, ``aspirate_delay``,
            ``dispense_delay``, and ``final_push_out`` parameters.
        """
        _log.debug(
            "mixing {}uL with {} repetitions in {} at rate={}".format(
                volume, repetitions, location if location else "current position", rate
            )
        )
        if not self._core.has_tip():
            raise UnexpectedTipRemovalError("mix", self.name, self.mount)

        if self.api_version >= APIVersion(2, 16):
            c_vol = self._core.get_available_volume() if volume is None else volume
        else:
            c_vol = self._core.get_available_volume() if not volume else volume

        if aspirate_flow_rate:
            if self.api_version < APIVersion(2, 24):
                raise APIVersionError(
                    api_element="aspirate_flow_rate",
                    until_version="2.24",
                    current_version=f"{self._api_version}",
                )
            if rate != 1.0:
                raise ValueError(
                    "rate must not be set if aspirate_flow_rate is specified"
                )
        if dispense_flow_rate:
            if self.api_version < APIVersion(2, 24):
                raise APIVersionError(
                    api_element="dispense_flow_rate",
                    until_version="2.24",
                    current_version=f"{self._api_version}",
                )
            if rate != 1.0:
                raise ValueError(
                    "rate must not be set if dispense_flow_rate is specified"
                )
        if aspirate_delay and self.api_version < APIVersion(2, 24):
            raise APIVersionError(
                api_element="aspirate_delay",
                until_version="2.24",
                current_version=f"{self._api_version}",
            )
        if dispense_delay and self.api_version < APIVersion(2, 24):
            raise APIVersionError(
                api_element="dispense_delay",
                until_version="2.24",
                current_version=f"{self._api_version}",
            )
        if final_push_out and self.api_version < APIVersion(2, 24):
            raise APIVersionError(
                api_element="final_push_out",
                until_version="2.24",
                current_version=f"{self._api_version}",
            )

        def delay_with_publish(seconds: float) -> None:
            # We don't have access to ProtocolContext.delay() which would automatically
            # publish a message to the broker, so we have to do it manually:
            with publisher.publish_context(
                broker=self.broker,
                command=protocol_cmds.delay(seconds=seconds, minutes=0, msg=None),
            ):
                self._protocol_core.delay(seconds=seconds, msg=None)

        def aspirate_with_delay(
            location: Optional[types.Location | labware.Well],
        ) -> None:
            self.aspirate(volume, location, rate, flow_rate=aspirate_flow_rate)
            if aspirate_delay:
                delay_with_publish(aspirate_delay)

        def dispense_with_delay(push_out: Optional[float]) -> None:
            self.dispense(
                volume, None, rate, flow_rate=dispense_flow_rate, push_out=push_out
            )
            if dispense_delay:
                delay_with_publish(dispense_delay)

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.mix(
                instrument=self,
                repetitions=repetitions,
                volume=c_vol,
                location=location,
            ),
        ):
            aspirate_with_delay(location=location)
            with AutoProbeDisable(self):
                while repetitions - 1 > 0:
                    # starting in 2.16, we disable push_out on all but the last
                    # dispense() to prevent the tip from jumping out of the liquid
                    # during the mix (PR #14004):
                    dispense_with_delay(
                        push_out=0 if self.api_version >= APIVersion(2, 16) else None
                    )
                    # aspirate location was set above, do subsequent aspirates in-place:
                    aspirate_with_delay(location=None)
                    repetitions -= 1
                if final_push_out is not None:
                    dispense_with_delay(push_out=final_push_out)
                else:
                    dispense_with_delay(push_out=None)
        return self

    @requires_version(2, 0)
    def blow_out(
        self,
        location: Optional[
            Union[types.Location, labware.Well, TrashBin, WasteChute]
        ] = None,
    ) -> InstrumentContext:
        """
        Blow an extra amount of air through a pipette's tip to clear it.

        If :py:meth:`dispense` is used to empty a pipette, usually a small amount of
        liquid remains in the tip. During a blowout, the pipette moves the plunger
        beyond its normal limits to help remove all liquid from the pipette tip. See
        :ref:`blow-out`.

        :param location: The blowout location. If no location is specified, the pipette
            will blow out from its current position.

            .. versionchanged:: 2.16
                Accepts ``TrashBin`` and ``WasteChute`` values.

        :type location: :py:class:`.Well` or :py:class:`.Location` or ``None``

        :raises RuntimeError: If no location is specified and the location cache is
                              ``None``. This should happen if ``blow_out()`` is called
                              without first calling a method that takes a location, like
                              :py:meth:`.aspirate` or :py:meth:`dispense`.
        :returns: This instance.

        .. versionchanged:: 2.24
            ``location`` is no longer required if the pipette just moved to, dispensed, or blew out
            into a trash bin or waste chute.
        """
        well: Optional[labware.Well] = None
        move_to_location: types.Location

        last_location = self._get_last_location_by_api_version()
        try:
            target = validation.validate_location(
                location=location, last_location=last_location
            )
        except validation.NoLocationError as e:
            raise RuntimeError(
                "If blow out is called without an explicit location, another"
                " method that moves to a location (such as move_to or "
                "dispense) must previously have been called so the robot "
                "knows where it is."
            ) from e

        if isinstance(target, validation.WellTarget):
            if target.well.parent.is_tiprack:
                _log.warning(
                    "Blow_out being performed on a tiprack. "
                    "Please re-check your code"
                )
            if target.location:
                # because the lower levels of blowout don't handle LiquidHandlingWellLocation and
                # there is no "operation_volume" for blowout we need to convert the relative location
                # given with a .meniscus to an absolute point. To maintain the meniscus behavior
                # we can just add the offset to the current liquid height.
                if target.location.meniscus_tracking:
                    move_to_location = target.well.bottom(
                        target.well.current_liquid_height()  # type: ignore [arg-type]
                        + target.location.point.z
                    )
                else:
                    move_to_location = target.location
            else:
                move_to_location = target.well.top()
            well = target.well
        elif isinstance(target, validation.PointTarget):
            move_to_location = target.location
        elif isinstance(target, validation.DisposalTarget):
            with publisher.publish_context(
                broker=self.broker,
                command=cmds.blow_out_in_disposal_location(
                    instrument=self, location=target.location
                ),
            ):
                self._core.blow_out(
                    location=target.location,
                    well_core=None,
                    in_place=target.in_place,
                )
            return self

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.blow_out(instrument=self, location=move_to_location),
        ):
            self._core.blow_out(
                location=move_to_location,
                well_core=well._core if well is not None else None,
                in_place=target.in_place,
            )

        return self

    def _determine_speed(self, speed: float) -> float:
        if self.api_version < APIVersion(2, 4):
            return clamp_value(speed, 80, 20, "touch_tip:")
        else:
            return clamp_value(speed, 80, 1, "touch_tip:")

    @publisher.publish(command=cmds.touch_tip)
    @requires_version(2, 0)
    def touch_tip(  # noqa: C901
        self,
        location: Optional[labware.Well] = None,
        radius: float = 1.0,
        v_offset: float = -1.0,
        speed: float = 60.0,
        mm_from_edge: Union[float, _Unset] = _Unset(),
    ) -> InstrumentContext:
        """
        Touch the pipette tip to the sides of a well, with the intent of removing leftover droplets.

        See :ref:`touch-tip` for more details and examples.

        :param location: If no location is passed, the pipette will touch its tip at the
                         edges of the current well.
        :type location: :py:class:`.Well` or ``None``
        :param radius: How far to move, as a proportion of the target well's radius.
                       When ``radius=1.0``, the pipette tip will move all the way to the
                       edge of the target well. When ``radius=0.5``, it will move to 50%
                       of the well's radius. Default is 1.0 (100%)
        :type radius: float
        :param v_offset: How far above or below the well to touch the tip, measured in mm.
                         A positive offset moves the tip higher above the well.
                         A negative offset moves the tip lower into the well.
                         Default is -1.0 mm.
        :type v_offset: float
        :param speed: The speed for touch tip motion, in mm/s.

                        - Default: 60.0 mm/s
                        - Maximum: 80.0 mm/s
                        - Minimum: 1.0 mm/s
        :type speed: float
        :param mm_from_edge: How far to move inside the well, as a distance from the
                             well's edge.
                             When ``mm_from_edge=0``, the pipette will move to the target well's edge to touch the tip. When ``mm_from_edge=1``,
                             the pipette will move to 1 mm from the target well's edge to touch the tip.
                             Values lower than 0 will press the tip harder into the target well's
                             walls; higher values will touch the well more lightly, or
                             not at all.
                             ``mm_from_edge`` and ``radius`` are mutually exclusive: to
                             use ``mm_from_edge``, ``radius`` must be unspecified (left
                             to its default value of 1.0).
        :type mm_from_edge: float
        :raises: ``UnexpectedTipRemovalError`` -- If no tip is attached to the pipette.
        :raises RuntimeError: If no location is specified and the location cache is
                              ``None``. This should happen if ``touch_tip`` is called
                              without first calling a method that takes a location, like
                              :py:meth:`.aspirate` or :py:meth:`dispense`.
        :raises: ValueError: If both ``mm_from_edge`` and ``radius`` are specified.
        :returns: This instance.

        .. versionchanged:: 2.24
                Added the ``mm_from_edge`` parameter.
        """
        if not self._core.has_tip():
            raise UnexpectedTipRemovalError("touch_tip", self.name, self.mount)

        checked_speed = self._determine_speed(speed)

        # If location is a valid well, move to the well first
        if location is None:
            last_location = self._protocol_core.get_last_location()
            if last_location is None or isinstance(
                last_location, (TrashBin, WasteChute)
            ):
                raise RuntimeError(
                    f"Cached location of {last_location} is not valid for touch tip."
                )
            parent_labware, well = last_location.labware.get_parent_labware_and_well()
            if not well or not parent_labware:
                raise RuntimeError(
                    f"Last location {location} has no associated well or labware."
                )
        elif isinstance(location, labware.Well):
            well = location
            parent_labware = well.parent
        else:
            raise TypeError(f"location should be a Well, but it is {location}")

        if not isinstance(mm_from_edge, _Unset):
            if self.api_version < APIVersion(2, 24):
                raise APIVersionError(
                    api_element="mm_from_edge",
                    until_version="2.24",
                    current_version=f"{self.api_version}",
                )
            if radius != 1.0:
                raise ValueError(
                    "radius must be set to 1.0 if mm_from_edge is specified"
                )

        if "touchTipDisabled" in parent_labware.quirks:
            _log.info(f"Ignoring touch tip on labware {well}")
            return self
        if parent_labware.is_tiprack:
            _log.warning(
                "Touch_tip being performed on a tiprack. Please re-check your code"
            )

        if self.api_version < APIVersion(2, 4):
            move_to_location = well.top()
        else:
            move_to_location = well.top(z=v_offset)

        self._core.touch_tip(
            location=move_to_location,
            well_core=well._core,
            radius=radius,
            z_offset=v_offset,
            speed=checked_speed,
            mm_from_edge=mm_from_edge if not isinstance(mm_from_edge, _Unset) else None,
        )
        return self

    @publisher.publish(command=cmds.air_gap)
    @requires_version(2, 0)
    def air_gap(  # noqa: C901
        self,
        volume: Optional[float] = None,
        height: Optional[float] = None,
        in_place: Optional[bool] = None,
        rate: Optional[float] = None,
        flow_rate: Optional[float] = None,
    ) -> InstrumentContext:
        """
        Draw air into the pipette's tip at the current well.

        See :ref:`air-gap`.

        :param volume: The amount of air, measured in µL. Calling ``air_gap()`` with no
                       arguments uses the entire remaining volume in the pipette.
        :type volume: float

        :param height: The height, in mm, to move above the current well before creating
                       the air gap. The default is 5 mm above the current well.
        :type height: float

        :param in_place: Air gap at the pipette's current position, without moving to
                         some height above the well. If ``in_place`` is specified,
                         ``height`` must be unset.
        :type in_place: bool

        :param rate: A multiplier for the default flow rate of the pipette. Calculated
                     as ``rate`` multiplied by :py:attr:`flow_rate.aspirate
                     <flow_rate>`. If neither rate nor flow_rate is specified, the pipette
                     will aspirate at a rate of 1.0 * InstrumentContext.flow_rate.aspirate. See
                     :ref:`new-plunger-flow-rates`.
        :type rate: float

        :param flow_rate: The rate, in µL/s, at which the pipette will draw in air.
        :type flow_rate: float

        :raises: ``UnexpectedTipRemovalError`` -- If no tip is attached to the pipette.

        :raises RuntimeError: If location cache is ``None`` and the air gap is not
                              ``in_place``. This would happen if ``air_gap()`` is called
                              without first calling a method that takes a location (e.g.,
                              :py:meth:`.aspirate`, :py:meth:`dispense`)

        :returns: This instance.

        Both ``volume`` and ``height`` are optional, but if you want to specify only
        ``height`` you must do it as a keyword argument:
        ``pipette.air_gap(height=2)``. If you call ``air_gap`` with a single,
        unnamed argument, it will always be interpreted as a volume.

        .. note::

           In API version 2.21 and earlier, this function was implemented as an aspirate, and
           dispensing into a well would add the air gap volume to the liquid tracked in
           the well. In API version 2.22 and later, air gap volume is not tracked as liquid
           when dispensing into a well.

        .. versionchanged:: 2.22
            No longer implemented as an aspirate.
        .. versionchanged:: 2.24
            Added the ``in_place`` option.
        .. versionchanged:: 2.24
            Adds the ``rate`` and ``flow_rate`` parameter. You can only define one or the other. If
            both are unspecified then ``rate`` is by default set to 1.0.
            Can air gap over a trash bin or waste chute.
        """
        if not self._core.has_tip():
            raise UnexpectedTipRemovalError("air_gap", self.name, self.mount)

        if rate is not None and self.api_version < APIVersion(2, 24):
            raise APIVersionError(
                api_element="rate",
                until_version="2.24",
                current_version=f"{self._api_version}",
            )

        if flow_rate is not None and self.api_version < APIVersion(2, 24):
            raise APIVersionError(
                api_element="flow_rate",
                until_version="2.24",
                current_version=f"{self._api_version}",
            )

        if flow_rate is not None and rate is not None:
            raise ValueError("Cannot define both flow_rate and rate.")

        if in_place:
            if self.api_version < APIVersion(2, 24):
                raise APIVersionError(
                    api_element="in_place",
                    until_version="2.24",
                    current_version=f"{self._api_version}",
                )
            if height is not None:
                raise ValueError("height must be unset if air gapping in_place")
        else:
            if height is None:
                height = 5
            last_location = self._protocol_core.get_last_location()
            if self.api_version < APIVersion(2, 24) and isinstance(
                last_location, (TrashBin, WasteChute)
            ):
                last_location = None
            if last_location is None or (
                isinstance(last_location, types.Location)
                and not last_location.labware.is_well
            ):
                raise RuntimeError(
                    f"Cached location of {last_location} is not valid for air gap."
                )
            target: Union[types.Location, TrashBin, WasteChute]
            if isinstance(last_location, types.Location):
                target = last_location.labware.as_well().top(height)
            else:
                target = last_location.top(height)
            self.move_to(target, publish=False)

        if self.api_version >= _AIR_GAP_TRACKING_ADDED_IN:
            self._core.prepare_to_aspirate()
            c_vol = self._core.get_available_volume() if volume is None else volume
            if flow_rate is not None:
                calculated_rate = flow_rate
            elif rate is not None:
                calculated_rate = rate * self._core.get_aspirate_flow_rate()
            else:
                calculated_rate = self._core.get_aspirate_flow_rate()

            self._core.air_gap_in_place(c_vol, calculated_rate)
        else:
            self.aspirate(volume)
        return self

    @publisher.publish(command=cmds.return_tip)
    @requires_version(2, 0)
    def return_tip(self, home_after: Optional[bool] = None) -> InstrumentContext:
        """
        Drop the currently attached tip in its original location in the tip rack.

        Returning a tip does not reset tip tracking, so :py:obj:`.Well.has_tip` will
        remain ``False`` for the destination.

        :returns: This instance.

        :param home_after: See the ``home_after`` parameter of :py:meth:`drop_tip`.
        """
        if not self._core.has_tip():
            _log.warning("Pipette has no tip to return")

        loc = self._last_tip_picked_up_from

        if not isinstance(loc, labware.Well):
            raise TypeError(f"Last tip location should be a Well but it is: {loc}")

        self.drop_tip(loc, home_after=home_after)

        return self

    @requires_version(2, 0)
    def pick_up_tip(  # noqa: C901
        self,
        location: Union[types.Location, labware.Well, labware.Labware, None] = None,
        presses: Optional[int] = None,
        increment: Optional[float] = None,
        prep_after: Optional[bool] = None,
    ) -> InstrumentContext:
        """
        Pick up a tip for the pipette to run liquid-handling commands.

        See :ref:`basic-tip-pickup`.

        If no location is passed, the pipette will pick up the next available tip in its
        :py:attr:`~.InstrumentContext.tip_racks` list. Within each tip rack, tips will
        be picked up in the order specified by the labware definition and
        :py:meth:`.Labware.wells`. To adjust where the sequence starts, use
        :py:obj:`.starting_tip`.

        The exact position for tip pickup accounts for the length of the tip and how
        much the tip overlaps with the pipette nozzle. These measurements are fixed
        values on Flex, and are based on the results of tip length calibration on OT-2.

        .. note::
            API version 2.19 updates the tip overlap values for Flex. When updating a
            protocol from 2.18 (or lower) to 2.19 (or higher), pipette performance
            should improve without additional changes to your protocol. Nevertheless, it
            is good practice after updating to do the following:

            - Run Labware Position Check.
            - Perform a dry run of your protocol.
            - If tip position is slightly higher than expected, adjust the ``location``
              parameter of pipetting actions to achieve the desired result.

        :param location: The location from which to pick up a tip. The ``location``
                         argument can be specified in several ways:

                           * As a :py:class:`.Well`. For example,
                             ``pipette.pick_up_tip(tiprack.wells()[0])`` will always pick
                             up the first tip in ``tiprack``, even if the rack is not a
                             member of :py:obj:`.InstrumentContext.tip_racks`.

                           * As a labware. ``pipette.pick_up_tip(tiprack)`` will pick up
                             the next available tip in ``tiprack``, even if the rack is
                             not a member of :py:obj:`.InstrumentContext.tip_racks`.

                           * As a :py:class:`~.types.Location`. Use this to make fine
                             adjustments to the pickup location. For example, to tell
                             the robot to start its pick up tip routine 1 mm closer to
                             the top of the well in the tip rack, call
                             ``pipette.pick_up_tip(tiprack["A1"].top(z=-1))``.
        :type location: :py:class:`.Well` or :py:class:`.Labware` or :py:class:`.types.Location`
        :param presses: The number of times to lower and then raise the pipette when
                        picking up a tip, to ensure a good seal. Zero (``0``) will
                        result in the pipette hovering over the tip but not picking it
                        up (generally not desirable, but could be used for a dry run).

                            .. deprecated:: 2.14
                                Use the Opentrons App to change pipette pick-up settings.
        :type presses: int
        :param increment: The additional distance to travel on each successive press.
                          For example, if ``presses=3`` and ``increment=1.0``, then the
                          first press will travel down into the tip by 3.5 mm, the
                          second by 4.5 mm, and the third by 5.5 mm).

                              .. deprecated:: 2.14
                                  Use the Opentrons App to change pipette pick-up settings.
        :type increment: float
        :param prep_after: Whether the pipette plunger should prepare itself to aspirate
                           immediately after picking up a tip.

                           If ``True``, the pipette will move its plunger position to
                           bottom in preparation for any following calls to
                           :py:meth:`.aspirate`.

                           If ``False``, the pipette will prepare its plunger later,
                           during the next call to :py:meth:`.aspirate`. This is
                           accomplished by moving the tip to the top of the well, and
                           positioning the plunger outside any potential liquids.

                           .. warning::
                               This is provided for compatibility with older Python
                               Protocol API behavior. You should normally leave this
                               unset.

                               Setting ``prep_after=False`` may create an unintended
                               pipette movement, when the pipette automatically moves
                               the tip to the top of the well to prepare the plunger.
        :type prep_after: bool

        .. versionchanged:: 2.13
            Adds the ``prep_after`` argument. In version 2.12 and earlier, the plunger
            can't prepare itself for aspiration during :py:meth:`.pick_up_tip`, and will
            instead always prepare during :py:meth:`.aspirate`. Version 2.12 and earlier
            will raise an ``APIVersionError`` if a value is set for ``prep_after``.

        .. versionchanged:: 2.19
            Uses new values for how much a tip overlaps with the pipette nozzle.

        :returns: This instance.
        """

        if presses is not None and self._api_version >= _PRESSES_INCREMENT_REMOVED_IN:
            raise UnsupportedAPIError(
                api_element="presses",
                since_version=f"{_PRESSES_INCREMENT_REMOVED_IN}",
                current_version=f"{self._api_version}",
            )

        if increment is not None and self._api_version >= _PRESSES_INCREMENT_REMOVED_IN:
            raise UnsupportedAPIError(
                api_element="increment",
                since_version=f"{_PRESSES_INCREMENT_REMOVED_IN}",
                current_version=f"{self._api_version}",
            )

        if prep_after is not None and self._api_version < _PREP_AFTER_ADDED_IN:
            raise APIVersionError(
                api_element="prep_after",
                until_version=f"{_PREP_AFTER_ADDED_IN}",
                current_version=f"{self._api_version}",
            )

        well: labware.Well
        tip_rack: labware.Labware
        move_to_location: Optional[types.Location] = None
        active_channels = (
            self.active_channels
            if self._api_version >= _PARTIAL_NOZZLE_CONFIGURATION_ADDED_IN
            else self.channels
        )
        nozzle_map = (
            self._core.get_nozzle_map()
            if self._api_version
            >= _PARTIAL_NOZZLE_CONFIGURATION_AUTOMATIC_TIP_TRACKING_IN
            else None
        )

        if location is None:
            if (
                nozzle_map is not None
                and nozzle_map.configuration != types.NozzleConfigurationType.FULL
                and self.starting_tip is not None
            ):
                # Disallowing this avoids concerning the system with the direction
                # in which self.starting_tip consumes tips. It would currently vary
                # depending on the configuration layout of a pipette at a given
                # time, which means that some combination of starting tip and partial
                # configuration are incompatible under the current understanding of
                # starting tip behavior. Replacing starting_tip with an un-deprecated
                # Labware.has_tip may solve this.
                raise CommandPreconditionViolated(
                    "Automatic tip tracking is not available when using a partial pipette"
                    " nozzle configuration and InstrumentContext.starting_tip."
                    " Switch to a full configuration or set starting_tip to None."
                )
            if not self._core.is_tip_tracking_available():
                raise CommandPreconditionViolated(
                    "Automatic tip tracking is not available for the current pipette"
                    " nozzle configuration. We suggest switching to a configuration"
                    " that supports automatic tip tracking or specifying the exact tip"
                    " to pick up."
                )
            tip_rack, well = labware.next_available_tip(
                starting_tip=self.starting_tip,
                tip_racks=self.tip_racks,
                channels=active_channels,
                nozzle_map=nozzle_map,
            )

        elif isinstance(location, labware.Well):
            well = location
            tip_rack = well.parent

        elif isinstance(location, labware.Labware):
            tip_rack, well = labware.next_available_tip(
                starting_tip=None,
                tip_racks=[location],
                channels=active_channels,
                nozzle_map=nozzle_map,
            )

        elif isinstance(location, types.Location):
            maybe_tip_rack, maybe_well = location.labware.get_parent_labware_and_well()

            if maybe_well is not None:
                well = maybe_well
                tip_rack = well.parent
                move_to_location = location

            elif maybe_tip_rack is not None:
                tip_rack, well = labware.next_available_tip(
                    starting_tip=None,
                    tip_racks=[maybe_tip_rack],
                    channels=active_channels,
                    nozzle_map=nozzle_map,
                )
            else:
                raise TypeError(
                    "If specified as a `types.Location`,"
                    " `location` should refer to a ``Labware` or `Well` location."
                    f" However, it refers to {location.labware}"
                )

        else:
            raise TypeError(
                "If specified, location should be an instance of"
                " `types.Location` (e.g. the return value from `Well.top()`),"
                "  `Labware` or `Well` (e.g. `tiprack.wells()[0]`)."
                f" However, it is {location}"
            )

        instrument.validate_tiprack(self.name, tip_rack, _log)

        move_to_location = move_to_location or well.top()
        prep_after = (
            prep_after
            if prep_after is not None
            else self.api_version >= _PREP_AFTER_ADDED_IN
        )

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.pick_up_tip(instrument=self, location=well),
        ):
            self._core.pick_up_tip(
                location=move_to_location,
                well_core=well._core,
                presses=presses,
                increment=increment,
                prep_after=prep_after,
            )

        self._last_tip_picked_up_from = well

        return self

    @requires_version(2, 0)
    def drop_tip(
        self,
        location: Optional[
            Union[
                types.Location,
                labware.Well,
                TrashBin,
                WasteChute,
            ]
        ] = None,
        home_after: Optional[bool] = None,
    ) -> InstrumentContext:
        """
        Drop the current tip.

        See :ref:`pipette-drop-tip` for examples.

        If no location is passed (e.g. ``pipette.drop_tip()``), the pipette will drop
        the attached tip into its :py:attr:`trash_container`.

        The location in which to drop the tip can be manually specified with the
        ``location`` argument. The ``location`` argument can be specified in several
        ways:

            - As a :py:class:`.Well`. This uses a default location relative to the well.
              This style of call can be used to make the robot drop a tip into labware
              like a well plate or a reservoir. For example,
              ``pipette.drop_tip(location=reservoir["A1"])``.
            - As a :py:class:`~.types.Location`. For example, to drop a tip from an
              unusually large height above the tip rack, you could call
              ``pipette.drop_tip(tip_rack["A1"].top(z=10))``.
            - As a :py:class:`.TrashBin`. This uses a default location relative to the
              ``TrashBin`` object. For example,
              ``pipette.drop_tip(location=trash_bin)``.
            - As a :py:class:`.WasteChute`. This uses a default location relative to
              the ``WasteChute`` object. For example,
              ``pipette.drop_tip(location=waste_chute)``.

        In API versions 2.15 to 2.17, if ``location`` is a ``TrashBin`` or not
        specified, the API will instruct the pipette to drop tips in different locations
        within the bin. Varying the tip drop location helps prevent tips
        from piling up in a single location.

        Starting with API version 2.18, the API will only vary the tip drop location if
        ``location`` is not specified. Specifying a ``TrashBin`` as the ``location``
        behaves the same as specifying :py:meth:`.TrashBin.top`, which is a fixed position.

        :param location:
            Where to drop the tip.

            .. versionchanged:: 2.16
                Accepts ``TrashBin`` and ``WasteChute`` values.

        :type location:
            :py:class:`~.types.Location` or :py:class:`.Well` or ``None``
        :param home_after:
            Whether to home the pipette's plunger after dropping the tip. If not
            specified, defaults to ``True`` on an OT-2.

            When ``False``, the pipette does not home its plunger. This can save a few
            seconds, but is not recommended. Homing helps the robot track the pipette's
            position.

        :returns: This instance.
        """
        alternate_drop_location: bool = False
        if location is None:
            trash_container = self.trash_container
            if self.api_version >= _DROP_TIP_LOCATION_ALTERNATING_ADDED_IN:
                alternate_drop_location = True
            if isinstance(trash_container, labware.Labware):
                well = trash_container.wells()[0]
            else:  # implicit drop tip in disposal location, not well
                with publisher.publish_context(
                    broker=self.broker,
                    command=cmds.drop_tip_in_disposal_location(
                        instrument=self, location=trash_container
                    ),
                ):
                    self._core.drop_tip_in_disposal_location(
                        trash_container,
                        home_after=home_after,
                        alternate_tip_drop=True,
                    )
                self._last_tip_picked_up_from = None
                return self

        elif isinstance(location, labware.Well):
            well = location
            location = None

        elif isinstance(location, types.Location):
            _, maybe_well = location.labware.get_parent_labware_and_well()

            if maybe_well is None:
                raise TypeError(
                    "If a location is specified as a `types.Location`"
                    " (for instance, as the result of a call to `Well.top()`),"
                    " it must be a location relative to a well,"
                    " since that is where a tip is dropped."
                    f" However, the given location refers to {location.labware}"
                )

            well = maybe_well

        elif isinstance(location, (TrashBin, WasteChute)):
            # In 2.16 and 2.17, we would always automatically use automatic alternate tip drop locations regardless
            # of whether you explicitly passed the disposal location as a location or if none was provided. Now, in
            # 2.18 and moving forward, passing it in will bypass the automatic behavior and instead go to the set
            # offset or the XY center if none is provided.
            if self.api_version < _DISPOSAL_LOCATION_OFFSET_ADDED_IN:
                alternate_drop_location = True
            with publisher.publish_context(
                broker=self.broker,
                command=cmds.drop_tip_in_disposal_location(
                    instrument=self, location=location
                ),
            ):
                self._core.drop_tip_in_disposal_location(
                    location,
                    home_after=home_after,
                    alternate_tip_drop=alternate_drop_location,
                )
            self._last_tip_picked_up_from = None
            return self

        else:
            raise TypeError(
                "If specified, location should be an instance of"
                " `types.Location` (e.g. the return value from `Well.top()`)"
                " or `Well` (e.g. `tiprack.wells()[0]`)."
                f" However, it is {location}"
            )

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.drop_tip(instrument=self, location=well),
        ):
            self._core.drop_tip(
                location=location,
                well_core=well._core,
                home_after=home_after,
                alternate_drop_location=alternate_drop_location,
            )

        self._last_tip_picked_up_from = None
        return self

    @requires_version(2, 0)
    def home(self) -> InstrumentContext:
        """Home the robot.

        See :ref:`utility-homing`.

        :returns: This instance.
        """

        mount_name = self._core.get_mount().name.lower()

        with publisher.publish_context(
            broker=self.broker, command=cmds.home(mount_name)
        ):
            self._core.home()

        return self

    @requires_version(2, 0)
    def home_plunger(self) -> InstrumentContext:
        """Home the plunger associated with this mount.

        :returns: This instance.
        """
        self._core.home_plunger()
        return self

    @publisher.publish(command=cmds.distribute)
    @requires_version(2, 0)
    def distribute(
        self,
        volume: Union[float, Sequence[float]],
        source: labware.Well,
        dest: List[labware.Well],
        *args: Any,
        **kwargs: Any,
    ) -> InstrumentContext:
        """
        Move a volume of liquid from one source to multiple destinations.

        :param volume: The amount, in µL, to dispense into each destination well.
        :param source: A single well to aspirate liquid from.
        :param dest: A list of wells to dispense liquid into.
        :param kwargs: See :py:meth:`transfer` and the :ref:`complex_params` page.
            Some parameters behave differently than when transferring.

              - ``disposal_volume`` aspirates additional liquid to improve the accuracy
                of each dispense. Defaults to the minimum volume of the pipette. See
                :ref:`param-disposal-volume` for details.

              - ``mix_after`` is ignored.


        :returns: This instance.
        """
        _log.debug("Distributing {} from {} to {}".format(volume, source, dest))
        kwargs["mode"] = "distribute"
        kwargs["disposal_volume"] = kwargs.get("disposal_volume", self.min_volume)
        kwargs["mix_after"] = (0, 0)
        blowout_location = kwargs.get("blowout_location")
        instrument.validate_blowout_location(
            self.api_version, "distribute", blowout_location
        )

        return self.transfer(volume, source, dest, **kwargs)

    @publisher.publish(command=cmds.consolidate)
    @requires_version(2, 0)
    def consolidate(
        self,
        volume: Union[float, Sequence[float]],
        source: List[labware.Well],
        dest: labware.Well,
        *args: Any,
        **kwargs: Any,
    ) -> InstrumentContext:
        """
        Move liquid from multiple source wells to a single destination well.

        :param volume: The amount, in µL, to aspirate from each source well.
        :param source: A list of wells to aspirate liquid from.
        :param dest: A single well to dispense liquid into.
        :param kwargs: See :py:meth:`transfer` and the :ref:`complex_params` page.
                       Some parameters behave differently than when transferring.
                       ``disposal_volume`` and ``mix_before`` are ignored.
        :returns: This instance.
        """
        _log.debug("Consolidate {} from {} to {}".format(volume, source, dest))
        kwargs["mode"] = "consolidate"
        kwargs["mix_before"] = (0, 0)
        kwargs["disposal_volume"] = 0
        blowout_location = kwargs.get("blowout_location")
        instrument.validate_blowout_location(
            self.api_version, "consolidate", blowout_location
        )

        return self.transfer(volume, source, dest, **kwargs)

    @publisher.publish(command=cmds.transfer)
    @requires_version(2, 0)
    def transfer(  # noqa: C901
        self,
        volume: Union[float, Sequence[float]],
        source: AdvancedLiquidHandling,
        dest: AdvancedLiquidHandling,
        trash: bool = True,
        **kwargs: Any,
    ) -> InstrumentContext:
        # source: Union[Well, List[Well], List[List[Well]]],
        # dest: Union[Well, List[Well], List[List[Well]]],
        # TODO: Reach consensus on kwargs
        # TODO: Decide if to use a disposal_volume
        # TODO: Accordingly decide if remaining liquid should be blown out to
        # TODO: ..trash or the original well.
        # TODO: What should happen if the user passes a non-first-row well
        # TODO: ..as src/dest *while using multichannel pipette?
        """
        Move liquid from one well or group of wells to another.

        Transfer is a higher-level command, incorporating other
        :py:class:`InstrumentContext` commands, like :py:meth:`aspirate` and
        :py:meth:`dispense`. It makes writing a protocol easier at the cost of
        specificity. See :ref:`v2-complex-commands` for details on how transfer and
        other complex commands perform their component steps.

        :param volume: The amount, in µL, to aspirate from each source and dispense to
                       each destination. If ``volume`` is a list, each amount will be
                       used for the source and destination at the matching index. A list
                       item of ``0`` will skip the corresponding wells entirely. See
                       :ref:`complex-list-volumes` for details and examples.
        :param source: A single well or a list of wells to aspirate liquid from.
        :param dest: A single well or a list of wells to dispense liquid into.

        :Keyword Arguments: Transfer accepts a number of optional parameters that give
            you greater control over the exact steps it performs. See
            :ref:`complex_params` or the links under each argument's entry below for
            additional details and examples.

            * **new_tip** (*string*) --
              When to pick up and drop tips during the command. Defaults to ``"once"``.

                - ``"once"``: Use one tip for the entire command.
                - ``"always"``: Use a new tip for each set of aspirate and dispense steps.
                - ``"never"``: Do not pick up or drop tips at all.

              See :ref:`param-tip-handling` for details.

            * **trash** (*boolean*) --
              If ``True`` (default), the pipette will drop tips in its
              :py:meth:`~.InstrumentContext.trash_container`.
              If ``False``, the pipette will return tips to their tip rack.

              See :ref:`param-trash` for details.

            * **touch_tip** (*boolean*) --
              If ``True``, perform a :py:meth:`touch_tip` following each
              :py:meth:`aspirate` and :py:meth:`dispense`. Defaults to ``False``.

              See :ref:`param-touch-tip` for details.

            * **blow_out** (*boolean*) --
              If ``True``, a :py:meth:`blow_out` will occur following each
              :py:meth:`dispense`, but only if the pipette has no liquid left
              in it. If ``False`` (default), the pipette will not blow out liquid.

              See :ref:`param-blow-out` for details.

            * **blowout_location** (*string*) --
              Accepts one of three string values: ``"trash"``, ``"source well"``, or
              ``"destination well"``.

              If ``blow_out`` is ``False`` (its default), this parameter is ignored.

              If ``blow_out`` is ``True`` and this parameter is not set:

                - Blow out into the trash, if the pipette is empty or only contains the
                  disposal volume.

                - Blow out into the source well, if the pipette otherwise contains liquid.

            * **mix_before** (*tuple*) --
              Perform a :py:meth:`mix` before each :py:meth:`aspirate` during the
              transfer. The first value of the tuple is the number of repetitions, and
              the second value is the amount of liquid to mix in µL.

              See :ref:`param-mix-before` for details.

            * **mix_after** (*tuple*) --
              Perform a :py:meth:`mix` after each :py:meth:`dispense` during the
              transfer. The first value of the tuple is the number of repetitions, and
              the second value is the amount of liquid to mix in µL.

              See :ref:`param-mix-after` for details.

            * **disposal_volume** (*float*) --
              Transfer ignores the numeric value of this parameter. If set, the pipette
              will not aspirate additional liquid, but it will perform a very small blow
              out after each dispense.

              See :ref:`param-disposal-volume` for details.

        :returns: This instance.
        """
        _log.debug("Transfer {} from {} to {}".format(volume, source, dest))

        blowout_location = kwargs.get("blowout_location")
        instrument.validate_blowout_location(
            self.api_version, "transfer", blowout_location
        )

        kwargs["mode"] = kwargs.get("mode", "transfer")

        mix_strategy, mix_opts = mix_from_kwargs(kwargs)

        if trash:
            drop_tip = v1_transfer.DropTipStrategy.TRASH
        else:
            drop_tip = v1_transfer.DropTipStrategy.RETURN

        new_tip = kwargs.get("new_tip")
        if isinstance(new_tip, str):
            new_tip = types.TransferTipPolicy[new_tip.upper()]

        blow_out = kwargs.get("blow_out")
        blow_out_strategy = None
        active_channels = (
            self.active_channels
            if self._api_version >= _PARTIAL_NOZZLE_CONFIGURATION_ADDED_IN
            else self.channels
        )
        nozzle_map = (
            self._core.get_nozzle_map()
            if self._api_version
            >= _PARTIAL_NOZZLE_CONFIGURATION_AUTOMATIC_TIP_TRACKING_IN
            else None
        )

        if blow_out and not blowout_location:
            if self.current_volume:
                blow_out_strategy = v1_transfer.BlowOutStrategy.SOURCE
            else:
                blow_out_strategy = v1_transfer.BlowOutStrategy.TRASH
        elif blow_out and blowout_location:
            if blowout_location == "source well":
                blow_out_strategy = v1_transfer.BlowOutStrategy.SOURCE
            elif blowout_location == "destination well":
                blow_out_strategy = v1_transfer.BlowOutStrategy.DEST
            elif blowout_location == "trash":
                blow_out_strategy = v1_transfer.BlowOutStrategy.TRASH

        if new_tip != types.TransferTipPolicy.NEVER:
            _, next_tip = labware.next_available_tip(
                self.starting_tip,
                self.tip_racks,
                active_channels,
                nozzle_map=nozzle_map,
            )
            max_volume = min(next_tip.max_volume, self.max_volume)
        else:
            max_volume = self._core.get_working_volume()

        touch_tip = None
        if kwargs.get("touch_tip"):
            touch_tip = v1_transfer.TouchTipStrategy.ALWAYS

        default_args = v1_transfer.Transfer()

        disposal = kwargs.get("disposal_volume")
        if disposal is None:
            disposal = default_args.disposal_volume

        air_gap = kwargs.get("air_gap", default_args.air_gap)
        if air_gap < 0 or air_gap >= max_volume:
            raise ValueError(
                "air_gap must be between 0uL and the pipette's expected "
                f"working volume, {max_volume}uL"
            )

        transfer_args = v1_transfer.Transfer(
            new_tip=new_tip or default_args.new_tip,
            air_gap=air_gap,
            carryover=kwargs.get("carryover") or default_args.carryover,
            gradient_function=(
                kwargs.get("gradient_function") or default_args.gradient_function
            ),
            disposal_volume=disposal,
            mix_strategy=mix_strategy,
            drop_tip_strategy=drop_tip,
            blow_out_strategy=blow_out_strategy or default_args.blow_out_strategy,
            touch_tip_strategy=(touch_tip or default_args.touch_tip_strategy),
        )
        transfer_options = v1_transfer.TransferOptions(
            transfer=transfer_args, mix=mix_opts
        )
        plan = v1_transfer.TransferPlan(
            volume,
            source,
            dest,
            self,
            max_volume,
            self.api_version,
            kwargs["mode"],
            transfer_options,
        )
        self._execute_transfer(plan)
        return self

    def _execute_transfer(self, plan: v1_transfer.TransferPlan) -> None:
        for cmd in plan:
            getattr(self, cmd["method"])(*cmd["args"], **cmd["kwargs"])

    @requires_version(2, 24)
    def transfer_with_liquid_class(
        self,
        liquid_class: LiquidClass,
        volume: float,
        source: Union[
            labware.Well, Sequence[labware.Well], Sequence[Sequence[labware.Well]]
        ],
        dest: Union[
            labware.Well,
            Sequence[labware.Well],
            Sequence[Sequence[labware.Well]],
            TrashBin,
            WasteChute,
        ],
        new_tip: TransferTipPolicyV2Type = "once",
        trash_location: Optional[
            Union[types.Location, labware.Well, TrashBin, WasteChute]
        ] = None,
        return_tip: bool = False,
        group_wells: bool = True,
        keep_last_tip: Optional[bool] = None,
    ) -> InstrumentContext:
        """Move a particular type of liquid from one well or group of wells to another.

        ..
            This is intended for Opentrons internal use only and is not a guaranteed API.

        :param liquid_class: The type of liquid to move. You must specify the liquid class,
            even if you have used :py:meth:`.Labware.load_liquid` to indicate what liquid the
            source contains.
        :type liquid_class: :py:class:`.LiquidClass`

        :param volume: The amount, in µL, to aspirate from each source and dispense to
                       each destination.
        :param source: A single well or a list of wells to aspirate liquid from.
        :param dest: A single well, list of wells, trash bin, or waste chute to dispense liquid into.
        :param new_tip: When to pick up and drop tips during the command.
            Defaults to ``"once"``.

              - ``"once"``: Use one tip for the entire command.
              - ``"always"``: Use a new tip for each set of aspirate and dispense steps.
              - ``"per source"``: Use one tip for each source well, even if
                :ref:`tip refilling <complex-tip-refilling>` is required.
              - ``"per destination"``: Use one tip for each destination well, even if
                :ref:`tip refilling <complex-tip-refilling>` is required.
              - ``"never"``: Do not pick up or drop tips at all.

            See :ref:`param-tip-handling` for details.

        :param trash_location: A trash container, well, or other location to dispose of
            tips. Depending on the liquid class, the pipette may also blow out liquid here.
            If not specified, the pipette will dispose of tips in its :py:obj:`~.InstrumentContext.trash_container`.
        :param return_tip: Whether to drop used tips in their original locations
            in the tip rack, instead of the trash.
        :param group_wells: For multi-channel transfers only. If set to ``True``, group together contiguous wells
            given into a single transfer step, taking into account the tip configuration. If ``False``, target
            each well given with the primary nozzle. Defaults to ``True``.
        :param keep_last_tip: When ``True``, the pipette keeps the last tip used in the transfer attached. When
            ``False``, the last tip will be dropped or returned. If not set, behavior depends on the value of
            ``new_tip``. ``new_tip="never"`` keeps the tip, and all other values of ``new_tip`` drop or return the tip.

        """
        if volume == 0.0:
            _log.info(
                f"Transfer of {liquid_class.name} specified with a volume of 0uL."
                f" Skipping."
            )
            return self

        transfer_args = verify_and_normalize_transfer_args(
            source=source,
            dest=dest,
            tip_policy=new_tip,
            last_tip_well=self._last_tip_picked_up_from,
            tip_racks=self._tip_racks,
            nozzle_map=self._core.get_nozzle_map(),
            group_wells_for_multi_channel=group_wells,
            current_volume=self.current_volume,
            trash_location=(
                trash_location if trash_location is not None else self.trash_container
            ),
        )
        verified_keep_last_tip = resolve_keep_last_tip(
            keep_last_tip, transfer_args.tip_policy
        )

        verified_dest: Union[
            List[Tuple[types.Location, WellCore]], TrashBin, WasteChute
        ]
        if isinstance(transfer_args.dest, (TrashBin, WasteChute)):
            verified_dest = transfer_args.dest
        else:
            if len(transfer_args.source) != len(transfer_args.dest):
                raise ValueError(
                    "Sources and destinations should be of the same length in order to perform a transfer."
                    " To transfer liquid from one source to many destinations, use 'distribute_liquid',"
                    " to transfer liquid to one destination from many sources, use 'consolidate_liquid'."
                )
            verified_dest = [
                (types.Location(types.Point(), labware=well), well._core)
                for well in transfer_args.dest
            ]

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.transfer_with_liquid_class(
                instrument=self,
                liquid_class=liquid_class,
                volume=volume,
                source=source,
                destination=dest,
            ),
        ):
            last_tip_location = self._core.transfer_with_liquid_class(
                liquid_class=liquid_class,
                volume=volume,
                source=[
                    (types.Location(types.Point(), labware=well), well._core)
                    for well in transfer_args.source
                ],
                dest=verified_dest,
                new_tip=transfer_args.tip_policy,
                tip_racks=[
                    (types.Location(types.Point(), labware=rack), rack._core)
                    for rack in transfer_args.tip_racks
                ],
                starting_tip=(
                    self.starting_tip._core if self.starting_tip is not None else None
                ),
                trash_location=transfer_args.trash_location,
                return_tip=return_tip,
                keep_last_tip=verified_keep_last_tip,
                last_tip_location=transfer_args.last_tip_location,
            )

        # TODO(jbl 2025-06-23) last_tip_picked_up_from should be removed from the public context and
        #   moved to the engine core or engine as a simpler and more holistic solution
        if last_tip_location is not None:
            tip_rack_loc, tip_well_core = last_tip_location
            self._last_tip_picked_up_from = tip_rack_loc.labware.as_labware()[
                tip_well_core.get_name()
            ]
        else:
            self._last_tip_picked_up_from = None

        return self

    @requires_version(2, 24)
    def distribute_with_liquid_class(
        self,
        liquid_class: LiquidClass,
        volume: float,
        source: Union[labware.Well, Sequence[labware.Well]],
        dest: Union[
            labware.Well, Sequence[labware.Well], Sequence[Sequence[labware.Well]]
        ],
        new_tip: TransferTipPolicyV2Type = "once",
        trash_location: Optional[
            Union[types.Location, labware.Well, TrashBin, WasteChute]
        ] = None,
        return_tip: bool = False,
        group_wells: bool = True,
        keep_last_tip: Optional[bool] = None,
    ) -> InstrumentContext:
        """
        Distribute a particular type of liquid from one well to a group of wells.

        ..
            This is intended for Opentrons internal use only and is not a guaranteed API.

        :param liquid_class: The type of liquid to move. You must specify the liquid class,
            even if you have used :py:meth:`.Labware.load_liquid` to indicate what liquid the
            source contains.
        :type liquid_class: :py:class:`.LiquidClass`

        :param volume: The amount, in µL, to aspirate from the source and dispense to
                       each destination.
        :param source: A single well for the pipette to target, or a group of wells to
                       target in a single aspirate for a multi-channel pipette.
        :param dest: A list of wells to dispense liquid into.
        :param new_tip: When to pick up and drop tips during the command.
            Defaults to ``"once"``.

              - ``"once"``: Use one tip for the entire command.
              - ``"always"``: Use a new tip before each aspirate.
              - ``"never"``: Do not pick up or drop tips at all.

            See :ref:`param-tip-handling` for details.

        :param trash_location: A trash container, well, or other location to dispose of
            tips. Depending on the liquid class, the pipette may also blow out liquid here.
            If not specified, the pipette will dispose of tips in its :py:obj:`~.InstrumentContext.trash_container`.
        :param return_tip: Whether to drop used tips in their original locations
            in the tip rack, instead of the trash.
        :param group_wells: For multi-channel transfers only. If set to ``True``, group together contiguous wells
            given into a single transfer step, taking into account the tip configuration. If ``False``, target
            each well given with the primary nozzle. Defaults to ``True``.
        :param keep_last_tip: When ``True``, the pipette keeps the last tip used in the distribute attached. When
            ``False``, the last tip will be dropped or returned. If not set, behavior depends on the value of
            ``new_tip``. ``new_tip="never"`` keeps the tip, and all other values of ``new_tip`` drop or return the tip.

        """
        if volume == 0.0:
            _log.info(
                f"Distribution of {liquid_class.name} specified with a volume of 0uL."
                f" Skipping."
            )
            return self

        transfer_args = verify_and_normalize_transfer_args(
            source=source,
            dest=dest,
            tip_policy=new_tip,
            last_tip_well=self._last_tip_picked_up_from,
            tip_racks=self._tip_racks,
            nozzle_map=self._core.get_nozzle_map(),
            group_wells_for_multi_channel=group_wells,
            current_volume=self.current_volume,
            trash_location=(
                trash_location if trash_location is not None else self.trash_container
            ),
        )
        verified_keep_last_tip = resolve_keep_last_tip(
            keep_last_tip, transfer_args.tip_policy
        )

        if isinstance(transfer_args.dest, (TrashBin, WasteChute)):
            raise ValueError(
                "distribute_with_liquid_class() does not support trash bin or waste chute"
                " as a destination."
            )
        if len(transfer_args.source) != 1:
            raise ValueError(
                f"Source should be a single well (or resolve to a single transfer for multi-channel) "
                f"but received {transfer_args.source}."
            )
        if transfer_args.tip_policy not in [
            TransferTipPolicyV2.ONCE,
            TransferTipPolicyV2.NEVER,
            TransferTipPolicyV2.ALWAYS,
        ]:
            raise ValueError(
                f"Incompatible `new_tip` value of {new_tip}."
                f" `distribute_with_liquid_class()` only supports `new_tip` values of"
                f" 'once', 'never' and 'always'."
            )

        verified_source = transfer_args.source[0]
        with publisher.publish_context(
            broker=self.broker,
            command=cmds.distribute_with_liquid_class(
                instrument=self,
                liquid_class=liquid_class,
                volume=volume,
                source=source,
                destination=dest,
            ),
        ):
            last_tip_location = self._core.distribute_with_liquid_class(
                liquid_class=liquid_class,
                volume=volume,
                source=(
                    types.Location(types.Point(), labware=verified_source),
                    verified_source._core,
                ),
                dest=[
                    (types.Location(types.Point(), labware=well), well._core)
                    for well in transfer_args.dest
                ],
                new_tip=transfer_args.tip_policy,  # type: ignore[arg-type]
                tip_racks=[
                    (types.Location(types.Point(), labware=rack), rack._core)
                    for rack in transfer_args.tip_racks
                ],
                starting_tip=(
                    self.starting_tip._core if self.starting_tip is not None else None
                ),
                trash_location=transfer_args.trash_location,
                return_tip=return_tip,
                keep_last_tip=verified_keep_last_tip,
                last_tip_location=transfer_args.last_tip_location,
            )

        # TODO(jbl 2025-06-23) last_tip_picked_up_from should be removed from the public context and
        #   moved to the engine core or engine as a simpler and more holistic solution
        if last_tip_location is not None:
            tip_rack_loc, tip_well_core = last_tip_location
            self._last_tip_picked_up_from = tip_rack_loc.labware.as_labware()[
                tip_well_core.get_name()
            ]
        else:
            self._last_tip_picked_up_from = None

        return self

    @requires_version(2, 24)
    def consolidate_with_liquid_class(
        self,
        liquid_class: LiquidClass,
        volume: float,
        source: Union[
            labware.Well, Sequence[labware.Well], Sequence[Sequence[labware.Well]]
        ],
        dest: Union[labware.Well, Sequence[labware.Well], TrashBin, WasteChute],
        new_tip: TransferTipPolicyV2Type = "once",
        trash_location: Optional[
            Union[types.Location, labware.Well, TrashBin, WasteChute]
        ] = None,
        return_tip: bool = False,
        group_wells: bool = True,
        keep_last_tip: Optional[bool] = None,
    ) -> InstrumentContext:
        """
        Consolidate a particular type of liquid from a group of wells to one well.

        ..
            This is intended for Opentrons internal use only and is not a guaranteed API.

        :param liquid_class: The type of liquid to move. You must specify the liquid class,
            even if you have used :py:meth:`.Labware.load_liquid` to indicate what liquid the
            source contains.
        :type liquid_class: :py:class:`.LiquidClass`

        :param volume: The amount, in µL, to aspirate from the source and dispense to
                       each destination.
        :param source: A list of wells to aspirate liquid from.
        :param dest: A single well, list of wells, trash bin, or waste chute to dispense liquid into.
                     Multiple wells can only be given for multi-channel pipette configurations, and
                     must be able to be dispensed to in a single dispense.
        :param new_tip: When to pick up and drop tips during the command.
            Defaults to ``"once"``.

              - ``"once"``: Use one tip for the entire command.
              - ``"always"``: Use a new tip after each aspirate and dispense, even when visiting the same source again.
              - ``"never"``: Do not pick up or drop tips at all.

            See :ref:`param-tip-handling` for details.

        :param trash_location: A trash container, well, or other location to dispose of
            tips. Depending on the liquid class, the pipette may also blow out liquid here.
            If not specified, the pipette will dispose of tips in its :py:obj:`~.InstrumentContext.trash_container`.
        :param return_tip: Whether to drop used tips in their original locations
            in the tip rack, instead of the trash.
        :param group_wells: For multi-channel transfers only. If set to ``True``, group together contiguous wells
            given into a single transfer step, taking into account the tip configuration. If ``False``, target
            each well given with the primary nozzle. Defaults to ``True``.
        :param keep_last_tip: When ``True``, the pipette keeps the last tip used in the consolidate attached. When
            ``False``, the last tip will be dropped or returned. If not set, behavior depends on the value of
            ``new_tip``. ``new_tip="never"`` keeps the tip, and all other values of ``new_tip`` drop or return the tip.

        """
        if volume == 0.0:
            _log.info(
                f"Consolidation of {liquid_class.name} specified with a volume of 0uL."
                f" Skipping."
            )
            return self

        transfer_args = verify_and_normalize_transfer_args(
            source=source,
            dest=dest,
            tip_policy=new_tip,
            last_tip_well=self._last_tip_picked_up_from,
            tip_racks=self._tip_racks,
            nozzle_map=self._core.get_nozzle_map(),
            group_wells_for_multi_channel=group_wells,
            current_volume=self.current_volume,
            trash_location=(
                trash_location if trash_location is not None else self.trash_container
            ),
        )
        verified_keep_last_tip = resolve_keep_last_tip(
            keep_last_tip, transfer_args.tip_policy
        )

        verified_dest: Union[Tuple[types.Location, WellCore], TrashBin, WasteChute]
        if isinstance(transfer_args.dest, (TrashBin, WasteChute)):
            verified_dest = transfer_args.dest
        else:
            if len(transfer_args.dest) != 1:
                raise ValueError(
                    f"Destination should be a single well (or resolve to a single transfer for multi-channel) "
                    f"but received {transfer_args.dest}."
                )
            verified_dest = (
                types.Location(types.Point(), labware=transfer_args.dest[0]),
                transfer_args.dest[0]._core,
            )
        if transfer_args.tip_policy not in [
            TransferTipPolicyV2.ONCE,
            TransferTipPolicyV2.NEVER,
            TransferTipPolicyV2.ALWAYS,
        ]:
            raise ValueError(
                f"Incompatible `new_tip` value of {new_tip}."
                f" `consolidate_with_liquid_class()` only supports `new_tip` values of"
                f" 'once', 'never' and 'always'."
            )

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.consolidate_with_liquid_class(
                instrument=self,
                liquid_class=liquid_class,
                volume=volume,
                source=source,
                destination=dest,
            ),
        ):
            last_tip_location = self._core.consolidate_with_liquid_class(
                liquid_class=liquid_class,
                volume=volume,
                source=[
                    (types.Location(types.Point(), labware=well), well._core)
                    for well in transfer_args.source
                ],
                dest=verified_dest,
                new_tip=transfer_args.tip_policy,  # type: ignore[arg-type]
                tip_racks=[
                    (types.Location(types.Point(), labware=rack), rack._core)
                    for rack in transfer_args.tip_racks
                ],
                starting_tip=(
                    self.starting_tip._core if self.starting_tip is not None else None
                ),
                trash_location=transfer_args.trash_location,
                return_tip=return_tip,
                keep_last_tip=verified_keep_last_tip,
                last_tip_location=transfer_args.last_tip_location,
            )

        # TODO(jbl 2025-06-23) last_tip_picked_up_from should be removed from the public context and
        #   moved to the engine core or engine as a simpler and more holistic solution
        if last_tip_location is not None:
            tip_rack_loc, tip_well_core = last_tip_location
            self._last_tip_picked_up_from = tip_rack_loc.labware.as_labware()[
                tip_well_core.get_name()
            ]
        else:
            self._last_tip_picked_up_from = None

        return self

    @requires_version(2, 0)
    def delay(self, *args: Any, **kwargs: Any) -> None:
        """
        .. deprecated:: 2.0
           Use :py:obj:`ProtocolContext.delay` instead.
           This method does nothing.
           It will be removed from a future version of the Python Protocol API.
        """
        if args or kwargs:
            # Former implementations of this method did not take any args, so users
            # would get a TypeError if they tried to call it like delay(minutes=10).
            # Without changing the ultimate behavior that such a call fails the
            # protocol, we can provide a more descriptive message as a courtesy.
            raise UnsupportedAPIError(
                message="InstrumentContext.delay() is not supported in Python Protocol API v2. Use ProtocolContext.delay() instead."
            )
        else:
            # Former implementations of this method, when called without any args,
            # called ProtocolContext.delay() with a duration of 0, which was
            # approximately a no-op.
            # Preserve that allowed way to call this method for the very remote chance
            # that a protocol out in the wild does it, for some reason.
            pass

    @requires_version(2, 0)
    def move_to(
        self,
        location: Union[types.Location, TrashBin, WasteChute],
        force_direct: bool = False,
        minimum_z_height: Optional[float] = None,
        speed: Optional[float] = None,
        publish: bool = True,
    ) -> InstrumentContext:
        """Move the instrument.

        See :ref:`move-to` for examples.

        :param location: Where to move to.

          .. versionchanged:: 2.16
               Accepts ``TrashBin`` and ``WasteChute`` values.

        :type location: :py:class:`~.types.Location`
        :param force_direct: If ``True``, move directly to the destination without arc
                             motion.

                             .. warning::
                                Forcing direct motion can cause the pipette to crash
                                into labware, modules, or other objects on the deck.

        :param minimum_z_height: An amount, measured in mm, to raise the mid-arc height.
                                 The mid-arc height can't be lowered.
        :param speed: The speed at which to move. By default,
                      :py:attr:`InstrumentContext.default_speed`. This controls the
                      straight linear speed of the motion. To limit individual axis
                      speeds, use :py:obj:`.ProtocolContext.max_speeds`.

        :param publish: Whether to list this function call in the run preview.
                        Default is ``True``.
        """
        with ExitStack() as contexts:
            if isinstance(location, (TrashBin, WasteChute)):
                if publish:
                    contexts.enter_context(
                        publisher.publish_context(
                            broker=self.broker,
                            command=cmds.move_to_disposal_location(
                                instrument=self, location=location
                            ),
                        )
                    )

                self._core.move_to(
                    location=location,
                    well_core=None,
                    force_direct=force_direct,
                    minimum_z_height=minimum_z_height,
                    speed=speed,
                    check_for_movement_conflicts=False,
                )
            else:
                if publish:
                    contexts.enter_context(
                        publisher.publish_context(
                            broker=self.broker,
                            command=cmds.move_to(instrument=self, location=location),
                        )
                    )

                _, well = location.labware.get_parent_labware_and_well()

                self._core.move_to(
                    location=location,
                    well_core=well._core if well is not None else None,
                    force_direct=force_direct,
                    minimum_z_height=minimum_z_height,
                    speed=speed,
                    check_for_movement_conflicts=False,
                )

        return self

    @requires_version(2, 23)
    def resin_tip_seal(
        self,
        location: Union[labware.Well, labware.Labware],
    ) -> InstrumentContext:
        """Seal resin tips onto the pipette.

        The location provided should contain resin tips. The pipette will attach itself
        to the resin tips but does not check any tip presence sensors. Before the pipette
        seals to the tips, the plunger will rise to the top of its working range so that
        it can perform a :py:func:`resin_tip_dispense` immediately.

        :param location: A location containing resin tips, must be a Labware or a Well.
        :type location: :py:class:`~.types.Location`
        """
        if isinstance(location, labware.Labware):
            well = location.wells()[0]
        else:
            well = location

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.seal(
                instrument=self,
                location=well,
            ),
        ):
            self._core.resin_tip_seal(
                location=well.top(), well_core=well._core, in_place=False
            )
        return self

    @requires_version(2, 23)
    def resin_tip_unseal(
        self,
        location: Union[labware.Well, labware.Labware],
    ) -> InstrumentContext:
        """Release resin tips from the pipette.

        The location provided should be a valid location to drop resin tips.

        :param location: A location containing that can accept tips.

        :type location: :py:class:`~.types.Location`

        :param home_after:
            Whether to home the pipette after dropping the tip. If not specified
            defaults to ``True`` on a Flex. The plunger will not home on an unseal.

            When ``False``, the pipette does not home its plunger. This can save a few
            seconds, but is not recommended. Homing helps the robot track the pipette's
            position.

        """
        if isinstance(location, labware.Labware):
            well = location.wells()[0]
        else:
            well = location

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.unseal(
                instrument=self,
                location=well,
            ),
        ):
            self._core.resin_tip_unseal(location=None, well_core=well._core)

        return self

    @requires_version(2, 23)
    def resin_tip_dispense(
        self,
        location: types.Location,
        volume: Optional[float] = None,
        rate: Optional[float] = None,
    ) -> InstrumentContext:
        """Push liquid out of resin tips that are currently sealed to a pipette.

        The volume and rate parameters for this function control the motion of the plunger
        to create a desired pressure profile inside the pipette chamber. Unlike a regular
        dispense action, the volume and rate do not correspond to liquid volume or flow rate
        dispensed from the resin tips. Select your values for volume and flow rate based on
        experimentation with the resin tips to create a pressure profile.

        The common way to use this function is as follows:

        #. Seal resin tips to the pipette using :py:meth:`InstrumentContext.resin_tip_seal`.

        #. Use :py:meth:`InstrumentContext.resin_tip_dispense` to displace an experimentally
           derived volume at an experimentally derived rate to create an experimentally derived
           target pressure inside the pipette.

        #. Use :py:meth:`ProtocolContext.delay` to wait an experimentally derived amount of
           time for the pressure inside the pipette to push liquid into and through the resin tip
           and out the other side.

        #. As liquid passes through the resin tip, the pressure inside the pipette will
           fall. If not all liquid has been dispensed from the resin tip, repeat steps 2
           and 3.

        #. Unseal resin tips from the pipette using :py:meth:`InstrumentContext.resin_tip_unseal`.

        Flex pipette pressure sensors will raise an overpressure when a differential pressure
        inside the pipette chamber above sensor limits is detected. You may need to disable the
        pressure sensor to create the required pressure profile.

        .. warning::
            Building excessive pressure inside the pipette chamber (significantly above the sensor
            limit) with the pressure sensors disabled can damage the pipette.


        :param location: Tells the robot where to dispense.
        :type location: :py:class:`~.types.Location`

        :param volume: The volume that the plunger should displace, in µL. Does not directly relate
                       to the volume of liquid that will be dispensed.
        :type volume: float

        :param rate: How quickly the plunger moves to displace the commanded volume, in µL/s. This rate does not directly relate to
                     the flow rate of liquid out of the resin tip.

                     Defaults to ``10.0`` µL/s.
        :type rate: float
        """
        well: Optional[labware.Well] = None
        last_location = self._get_last_location_by_api_version()

        try:
            target = validation.validate_location(
                location=location, last_location=last_location
            )
        except validation.NoLocationError as e:
            raise RuntimeError(
                "If dispense is called without an explicit location, another"
                " method that moves to a location (such as move_to or "
                "aspirate) must previously have been called so the robot "
                "knows where it is."
            ) from e

        if isinstance(target, validation.WellTarget):
            well = target.well
            if target.location:
                move_to_location = target.location
            elif well.parent._core.is_fixed_trash():
                move_to_location = target.well.top()
            else:
                move_to_location = target.well.bottom(
                    z=self._well_bottom_clearances.dispense
                )
        else:
            raise RuntimeError(
                "A well must be specified when using `resin_tip_dispense`."
            )

        with publisher.publish_context(
            broker=self.broker,
            command=cmds.resin_tip_dispense(
                instrument=self,
                flow_rate=rate,
            ),
        ):
            self._core.resin_tip_dispense(
                move_to_location,
                well_core=well._core,
                volume=volume,
                flow_rate=rate,
            )
        return self

    @requires_version(2, 18)
    def _retract(
        self,
    ) -> None:
        self._core.retract()

    @property
    @requires_version(2, 0)
    def mount(self) -> str:
        """
        Return the name of the mount the pipette is attached to.

        The possible names are ``"left"`` and ``"right"``.
        """
        return self._core.get_mount().name.lower()

    @property
    @requires_version(2, 0)
    def speed(self) -> "PlungerSpeeds":
        """The speeds (in mm/s) configured for the pipette plunger.

        This is an object with attributes ``aspirate``, ``dispense``, and ``blow_out``
        holding the plunger speeds for the corresponding operation.

        .. note::
          Setting values of :py:attr:`flow_rate` will override the values in
          :py:attr:`speed`.

        .. versionchanged:: 2.14
            This property has been removed because it's fundamentally misaligned with
            the step-wise nature of a pipette's plunger speed configuration. Use
            :py:attr:`.flow_rate` instead.
        """
        if self._api_version >= ENGINE_CORE_API_VERSION:
            raise UnsupportedAPIError(
                message="InstrumentContext.speed has been removed. Use InstrumentContext.flow_rate, instead."
            )

        # TODO(mc, 2023-02-13): this assert should be enough for mypy
        # investigate if upgrading mypy allows the `cast` to be removed
        assert isinstance(self._core, LegacyInstrumentCore)
        return cast(LegacyInstrumentCore, self._core).get_speed()

    @property
    @requires_version(2, 0)
    def flow_rate(self) -> "FlowRates":
        """The speeds, in µL/s, configured for the pipette.

        See :ref:`new-plunger-flow-rates`.

        This is an object with attributes ``aspirate``, ``dispense``, and ``blow_out``
        holding the flow rate for the corresponding operation.

        .. note::
          Setting values of :py:attr:`speed`, which is deprecated, will override the
          values in :py:attr:`flow_rate`.

        """
        return self._core.get_flow_rate()

    @property
    @requires_version(2, 0)
    def type(self) -> str:
        """``'single'`` if this is a 1-channel pipette, or ``'multi'`` otherwise.

        See also :py:obj:`.channels`, which can distinguish between 8-channel and 96-channel
        pipettes.
        """
        if self.channels == 1:
            return "single"
        else:
            return "multi"

    @property
    @requires_version(2, 0)
    def tip_racks(self) -> List[labware.Labware]:
        """
        The tip racks that have been linked to this pipette.

        This is the property used to determine which tips to pick up next when calling
        :py:meth:`pick_up_tip` without arguments. See :ref:`basic-tip-pickup`.
        """
        return self._tip_racks

    @tip_racks.setter
    def tip_racks(self, racks: List[labware.Labware]) -> None:
        self._tip_racks = racks

    @property
    @requires_version(2, 20)
    def liquid_presence_detection(self) -> bool:
        """
        Whether the pipette will perform automatic liquid presence detection.

        When ``True``, the pipette will check for liquid on every aspiration.
        Defaults to ``False``. See :ref:`lpd`.
        """
        return self._core.get_liquid_presence_detection()

    @liquid_presence_detection.setter
    @requires_version(2, 20)
    def liquid_presence_detection(self, enable: bool) -> None:
        if enable:
            self._raise_if_pressure_not_supported_by_pipette()
        self._core.set_liquid_presence_detection(enable)

    @property
    @requires_version(2, 0)
    def trash_container(self) -> Union[labware.Labware, TrashBin, WasteChute]:
        """The trash container associated with this pipette.

        This is the property used to determine where to drop tips and blow out liquids
        when calling :py:meth:`drop_tip` or :py:meth:`blow_out` without arguments.

        You can set this to a :py:obj:`Labware`, :py:class:`.TrashBin`, or :py:class:`.WasteChute`.

        The default value depends on the robot type and API version:

        - :py:obj:`ProtocolContext.fixed_trash`, if it exists.
        - Otherwise, the first item previously loaded with
          :py:obj:`ProtocolContext.load_trash_bin()` or
          :py:obj:`ProtocolContext.load_waste_chute()`.

        .. versionchanged:: 2.16
            Added support for ``TrashBin`` and ``WasteChute`` objects.
        """
        if self._user_specified_trash is None:
            disposal_locations = self._protocol_core.get_disposal_locations()
            if len(disposal_locations) == 0:
                raise NoTrashDefinedError(
                    "No trash container has been defined in this protocol."
                )
            return disposal_locations[0]
        return self._user_specified_trash

    @trash_container.setter
    def trash_container(
        self, trash: Union[labware.Labware, TrashBin, WasteChute]
    ) -> None:
        self._user_specified_trash = trash

    @property
    @requires_version(2, 0)
    def name(self) -> str:
        """
        The name string for the pipette.

        From API version 2.15 to 2.22, this property returned an internal name for Flex
        pipettes. (e.g., ``"p1000_single_flex"``).

        In API version 2.23 and later, this property returns the Python Protocol API
        :ref:`load name <new-pipette-models>` of Flex pipettes (e.g.,
        ``"flex_1channel_1000"``).
        """
        return self._core.get_pipette_name()

    @property
    @requires_version(2, 0)
    def model(self) -> str:
        """
        The model string for the pipette (e.g., ``'p300_single_v1.3'``)
        """
        return self._core.get_model()

    @property
    @requires_version(2, 0)
    def min_volume(self) -> float:
        """
        The minimum volume, in µL, that the pipette can hold. This value may change
        based on the :ref:`volume mode <pipette-volume-modes>` that the pipette is
        currently configured for.
        """
        return self._core.get_min_volume()

    @property
    @requires_version(2, 0)
    def max_volume(self) -> float:
        """
        The maximum volume, in µL, that the pipette can hold.

        The maximum volume that you can actually aspirate might be lower than this,
        depending on what kind of tip is attached to this pipette. For example, a P300
        Single-Channel pipette always has a ``max_volume`` of 300 µL, but if it's using
        a 200 µL filter tip, its usable volume would be limited to 200 µL.
        """
        return self._core.get_max_volume()

    @property
    @requires_version(2, 0)
    def current_volume(self) -> float:
        """
        The current amount of liquid held in the pipette, measured in µL.
        """
        return self._core.get_current_volume()

    @property
    @requires_version(2, 7)
    def has_tip(self) -> bool:
        """Whether this instrument has a tip attached or not.

        The value of this property is determined logically by the API, not by detecting
        the physical presence of a tip. This is the case even on Flex, which has sensors
        to detect tip attachment.
        """
        return self._core.has_tip()

    @property
    def _has_tip(self) -> bool:
        """
        Internal function used to check whether this instrument has a
        tip attached or not.
        """
        return self._core.has_tip()

    @property
    @requires_version(2, 0)
    def hw_pipette(self) -> PipetteDict:
        """View the information returned by the hardware API directly.

        :raises: :py:class:`.types.PipetteNotAttachedError` if the pipette is
                 no longer attached (should not happen).
        """
        return self._core.get_hardware_state()

    @property
    @requires_version(2, 0)
    def channels(self) -> int:
        """The number of channels on the pipette.

        Possible values are 1, 8, or 96.

        See also :py:obj:`.type`.
        """
        return self._core.get_channels()

    @property
    @requires_version(2, 16)
    def active_channels(self) -> int:
        """The number of channels the pipette will use to pick up tips.

        By default, all channels on the pipette. Use :py:meth:`.configure_nozzle_layout`
        to set the pipette to use fewer channels.
        """
        return self._core.get_active_channels()

    @property
    @requires_version(2, 2)
    def return_height(self) -> float:
        """The height to return a tip to its tip rack.

        :returns: A scaling factor to apply to the tip length.
                  During :py:meth:`.drop_tip`, this factor is multiplied by the tip
                  length to get the distance from the top of the well to drop the tip.
        """
        return self._core.get_return_height()

    @property
    @requires_version(2, 0)
    def well_bottom_clearance(self) -> "Clearances":
        """The distance above the bottom of a well to aspirate or dispense.

        This is an object with attributes ``aspirate`` and ``dispense``, describing the
        default height of the corresponding operation. The default is 1.0 mm for both
        aspirate and dispense.

        When :py:meth:`aspirate` or :py:meth:`dispense` is given a :py:class:`.Well`
        rather than a full :py:class:`.Location`, the robot will move this distance
        above the bottom of the well to aspirate or dispense.

        To change, set the corresponding attribute::

            pipette.well_bottom_clearance.aspirate = 2

        """
        return self._well_bottom_clearances

    def _get_last_location_by_api_version(
        self,
    ) -> Optional[Union[types.Location, TrashBin, WasteChute]]:
        """Get the last location accessed by this pipette, if any.

        In pre-engine Protocol API versions, this call omits the pipette mount.
        Between 2.14 (first engine PAPI version) and 2.23 this only returns None or a Location object.
        This is to preserve pre-existing, potentially buggy behavior.
        """
        if self._api_version >= APIVersion(2, 24):
            return self._protocol_core.get_last_location(mount=self._core.get_mount())
        elif self._api_version >= ENGINE_CORE_API_VERSION:
            last_location = self._protocol_core.get_last_location(
                mount=self._core.get_mount()
            )
            return last_location if isinstance(last_location, types.Location) else None
        else:
            return self._protocol_core.get_last_location()

    def __repr__(self) -> str:
        return "<{}: {} in {}>".format(
            self.__class__.__name__,
            self._core.get_model(),
            self._core.get_mount().name,
        )

    def __str__(self) -> str:
        return "{} on {} mount".format(self._core.get_display_name(), self.mount)

    @publisher.publish(command=cmds.configure_for_volume)
    @requires_version(2, 15)
    def configure_for_volume(self, volume: float) -> None:
        """Configure a pipette to handle a specific volume of liquid, measured in µL.
        The pipette enters a volume mode depending on the volume provided. Changing
        pipette modes alters properties of the instance of
        :py:class:`.InstrumentContext`, such as default flow rate, minimum volume, and
        maximum volume. The pipette remains in the mode set by this function until it is
        called again.

        The Flex 1-Channel 50 µL and Flex 8-Channel 50 µL pipettes must operate in a
        low-volume mode to accurately dispense very small volumes of liquid. Low-volume
        mode can only be set by calling ``configure_for_volume()``. See
        :ref:`pipette-volume-modes`.

        .. note ::

            Changing a pipette's mode will reset its :ref:`flow rates
            <new-plunger-flow-rates>`.

        This function will raise an error if called when the pipette's tip contains
        liquid. It won't raise an error if a tip is not attached, but changing modes may
        affect which tips the pipette can subsequently pick up without raising an error.

        This function will also raise an error if ``volume`` is outside of the
        :ref:`minimum and maximum capacities <new-pipette-models>` of the pipette (e.g.,
        setting ``volume=1`` for a Flex 1000 µL pipette).

        :param volume: The volume, in µL, that the pipette will prepare to handle.
        :type volume: float
        """
        if self._core.get_current_volume():
            raise CommandPreconditionViolated(
                message=f"Cannot switch modes of {str(self)} while it contains liquid"
            )
        if volume < 0:
            raise CommandParameterLimitViolated(
                command_name="configure_for_volume",
                parameter_name="volume",
                limit_statement="must be greater than 0",
                actual_value=str(volume),
            )
        last_location = self._get_last_location_by_api_version()
        if (
            last_location
            and isinstance(last_location, types.Location)
            and isinstance(last_location.labware, labware.Well)
        ):
            self.move_to(last_location.labware.top())
        self._core.configure_for_volume(volume)

    @requires_version(2, 16)
    def prepare_to_aspirate(self) -> None:
        """Prepare a pipette for aspiration.

        Before a pipette can aspirate into an empty tip, the plunger must be in its
        bottom position. After dropping a tip or blowing out, the plunger will be in a
        different position. This function moves the plunger to the bottom position,
        regardless of its current position, to make sure that the pipette is ready to
        aspirate.

        You rarely need to call this function. The API automatically prepares the
        pipette for aspiration as part of other commands:

            - After picking up a tip with :py:meth:`.pick_up_tip`.
            - When calling :py:meth:`.aspirate`, if the pipette isn't already prepared.
              If the pipette is in a well, it will move out of the well, move the plunger,
              and then move back.

        Use ``prepare_to_aspirate()`` when you need to control exactly when the plunger
        motion will happen. A common use case is a pre-wetting routine, which requires
        preparing for aspiration, moving into a well, and then aspirating *without
        leaving the well*::

             pipette.move_to(well.bottom(z=2))
             protocol.delay(5)
             pipette.mix(10, 10)
             pipette.move_to(well.top(z=5))
             pipette.blow_out()
             pipette.prepare_to_aspirate()
             pipette.move_to(well.bottom(z=2))
             protocol.delay(5)
             pipette.aspirate(10, well.bottom(z=2))

        The call to ``prepare_to_aspirate()`` means that the plunger will be in the
        bottom position before the call to ``aspirate()``. Since it doesn't need to
        prepare again, it will not move up out of the well to move the plunger. It will
        aspirate in place.
        """
        if self._core.get_current_volume():
            raise CommandPreconditionViolated(
                message=f"Cannot prepare {str(self)} for aspirate while it contains liquid."
            )
        self._core.prepare_to_aspirate()

    @publisher.publish(command=cmds.configure_nozzle_layout)
    @requires_version(2, 16)
    def configure_nozzle_layout(
        self,
        style: NozzleLayout,
        start: Optional[str] = None,
        end: Optional[str] = None,
        front_right: Optional[str] = None,
        back_left: Optional[str] = None,
        tip_racks: Optional[List[labware.Labware]] = None,
    ) -> None:
        """Configure how many tips the 8-channel or 96-channel pipette will pick up.

        Changing the nozzle layout will affect gantry movement for all subsequent
        pipetting actions that the pipette performs. It also alters the pipette's
        behavior for picking up tips. The pipette will continue to use the specified
        layout until this function is called again.

        .. note::
            When picking up fewer than 96 tips at once, the tip rack *must not* be
            placed in a tip rack adapter in the deck. If you try to pick up fewer than 96
            tips from a tip rack that is in an adapter, the API will raise an error.

        :param style: The shape of the nozzle layout.
            You must :ref:`import the layout constant <nozzle-layouts>` in order to use it.

            - ``ALL`` resets the pipette to use all of its nozzles. Calling
              ``configure_nozzle_layout`` with no arguments also resets the pipette.
            - ``COLUMN`` sets a 96-channel pipette to use 8 nozzles, aligned from front to back
              with respect to the deck. This corresponds to a column of wells on labware.
              For 8-channel pipettes, use ``ALL`` instead.
            - ``PARTIAL_COLUMN`` sets an 8-channel pipette to use 2--7 nozzles, aligned from front to back
              with respect to the deck. Not compatible with the 96-channel pipette.
            - ``ROW`` sets a 96-channel pipette to use 12 nozzles, aligned from left to right
              with respect to the deck. This corresponds to a row of wells on labware.
              Not compatible with 8-channel pipettes.
            - ``SINGLE`` sets the pipette to use 1 nozzle. This corresponds to a single well on labware.

        :type style: ``NozzleLayout`` or ``None``
        :param start: The primary nozzle of the layout, which the robot uses
            to determine how it will move to different locations on the deck. The string
            should be of the same format used when identifying wells by name.
            Required unless setting ``style=ALL``.

            .. note::
                If possible, don't use both ``start="A1"`` and ``start="A12"`` to pick up
                tips *from the same rack*. Doing so can affect positional accuracy.

        :type start: str or ``None``
        :param end: The nozzle at the end of a linear layout, which is used
            to determine how many tips will be picked up by a pipette. The string
            should be of the same format used when identifying wells by name.
            Required when setting ``style=PARTIAL_COLUMN``.

        :type end: str or ``None``
        :param tip_racks: Behaves the same as setting the ``tip_racks`` parameter of
            :py:meth:`.load_instrument`. If not specified, the new configuration resets
            :py:obj:`.InstrumentContext.tip_racks` and you must specify the location
            every time you call :py:meth:`~.InstrumentContext.pick_up_tip`.
        :type tip_racks: List[:py:class:`.Labware`]

        .. versionchanged:: 2.20
            Added partial column, row, and single layouts.
        """
        #       TODO: add the following back into the docstring when QUADRANT is supported
        #
        #       :param front_right: The nozzle at the front left of the layout. Only used for
        #           NozzleLayout.QUADRANT configurations.
        #       :type front_right: str or ``None``
        #
        #       NOTE: Disabled layouts error case can be removed once desired map configurations
        #       have appropriate data regarding tip-type to map current values added to the
        #       pipette definitions.

        disabled_layouts = [
            NozzleLayout.QUADRANT,
        ]
        if style in disabled_layouts:
            # todo(mm, 2024-08-20): UnsupportedAPIError boils down to an API_REMOVED
            # error code, which is not correct here.
            raise UnsupportedAPIError(
                message=f"Nozzle layout configuration of style {style.value} is currently unsupported."
            )

        original_enabled_layouts = [NozzleLayout.COLUMN, NozzleLayout.ALL]
        if (
            self._api_version
            < _PARTIAL_NOZZLE_CONFIGURATION_SINGLE_ROW_PARTIAL_COLUMN_ADDED_IN
        ) and (style not in original_enabled_layouts):
            raise APIVersionError(
                api_element=f"Nozzle layout configuration of style {style.value}",
                until_version=str(
                    _PARTIAL_NOZZLE_CONFIGURATION_SINGLE_ROW_PARTIAL_COLUMN_ADDED_IN
                ),
                current_version=str(self._api_version),
            )

        front_right_resolved = front_right
        back_left_resolved = back_left
        validated_start: Optional[str] = None
        match style:
            case NozzleLayout.SINGLE:
                validated_start = _check_valid_start_nozzle(style, start)
                _raise_if_has_end_or_front_right_or_back_left(
                    style, end, front_right, back_left
                )
            case NozzleLayout.COLUMN | NozzleLayout.ROW:
                self._raise_if_configuration_not_supported_by_pipette(style)
                validated_start = _check_valid_start_nozzle(style, start)
                _raise_if_has_end_or_front_right_or_back_left(
                    style, end, front_right, back_left
                )
            case NozzleLayout.PARTIAL_COLUMN:
                self._raise_if_configuration_not_supported_by_pipette(style)
                validated_start = _check_valid_start_nozzle(style, start)
                validated_end = _check_valid_end_nozzle(validated_start, end)
                _raise_if_has_front_right_or_back_left_for_partial_column(
                    front_right, back_left
                )
                # Convert 'validated_end' to front_right or back_left as appropriate
                if validated_start == "H1" or validated_start == "H12":
                    back_left_resolved = validated_end
                    front_right_resolved = validated_start
                elif start == "A1" or start == "A12":
                    front_right_resolved = validated_end
                    back_left_resolved = validated_start
            case NozzleLayout.QUADRANT:
                validated_start = _check_valid_start_nozzle(style, start)
                _raise_if_has_end_nozzle_for_quadrant(end)
                _raise_if_no_front_right_or_back_left_for_quadrant(
                    front_right, back_left
                )
                if front_right is None:
                    front_right_resolved = validated_start
                elif back_left is None:
                    back_left_resolved = validated_start
            case NozzleLayout.ALL:
                validated_start = start
                if any([start, end, front_right, back_left]):
                    _log.warning(
                        "Parameters 'start', 'end', 'front_right', 'back_left' specified"
                        " for ALL nozzle configuration will be ignored."
                    )

        self._core.configure_nozzle_layout(
            style,
            primary_nozzle=validated_start,
            front_right_nozzle=front_right_resolved,
            back_left_nozzle=back_left_resolved,
        )
        self._tip_racks = tip_racks or []

    @requires_version(2, 20)
    def detect_liquid_presence(self, well: labware.Well) -> bool:
        """Checks for liquid in a well.

        Returns ``True`` if liquid is present and ``False`` if liquid is not present. Will not raise an error if it does not detect liquid. When simulating a protocol, the check always succeeds (returns ``True``). Works with Flex 1-, 8-, and 96-channel pipettes. See :ref:`detect-liquid-presence`.

        .. note::
            The pressure sensors for the Flex 8-channel pipette are on channels 1 and 8 (positions A1 and H1). For the Flex 96-channel pipette, the pressure sensors are on channels 1 and 96 (positions A1 and H12). Other channels on multi-channel pipettes do not have sensors and cannot detect liquid.
        """
        self._raise_if_pressure_not_supported_by_pipette()
        loc = well.top()
        return self._core.detect_liquid_presence(well._core, loc)

    @requires_version(2, 20)
    def require_liquid_presence(self, well: labware.Well) -> None:
        """Check for liquid in a well and raises an error if none is detected.

        When this method raises an error, Flex will offer the opportunity to enter recovery mode. In recovery mode, you can manually add liquid to resolve the error. When simulating a protocol, the check always succeeds (does not raise an error). Works with Flex 1-, 8-, and 96-channel pipettes. See :ref:`lpd` and :ref:`require-liquid-presence`.

        .. note::
            The pressure sensors for the Flex 8-channel pipette are on channels 1 and 8 (positions A1 and H1). For the Flex 96-channel pipette, the pressure sensors are on channels 1 and 96 (positions A1 and H12). Other channels on multi-channel pipettes do not have sensors and cannot detect liquid.
        """
        self._raise_if_pressure_not_supported_by_pipette()
        loc = well.top()
        self._core.liquid_probe_with_recovery(well._core, loc)

    @requires_version(2, 20)
    def measure_liquid_height(self, well: labware.Well) -> LiquidTrackingType:
        """Check the height of the liquid within a well.

        :returns: The height, in mm, of the liquid from the bottom of the well.
        """
        self._raise_if_pressure_not_supported_by_pipette()
        loc = well.top()
        self._core.liquid_probe_with_recovery(well._core, loc)
        return well.current_liquid_height()

    def _raise_if_configuration_not_supported_by_pipette(
        self, style: NozzleLayout
    ) -> None:
        match style:
            case NozzleLayout.COLUMN | NozzleLayout.ROW:
                if self.channels != 96:
                    raise ValueError(
                        f"{style.value} configuration is only supported on 96-Channel pipettes."
                    )
            case NozzleLayout.PARTIAL_COLUMN:
                if self.channels != 8:
                    raise ValueError(
                        "Partial column configuration is only supported on 8-Channel pipettes."
                    )
            # SINGLE, QUADRANT and ALL are supported by all pipettes

    def _raise_if_pressure_not_supported_by_pipette(self) -> None:
        if not self._core._pressure_supported_by_pipette():
            raise UnsupportedHardwareCommand(
                "Pressure sensor not available for this pipette"
            )

    def _handle_aspirate_target(
        self, target: Union[validation.WellTarget, validation.PointTarget]
    ) -> tuple[
        types.Location, Optional[labware.Well], Optional[types.MeniscusTrackingTarget]
    ]:
        if isinstance(target, validation.WellTarget):
            if target.location:
                return target.location, target.well, target.location.meniscus_tracking

            else:
                return (
                    target.well.bottom(z=self._well_bottom_clearances.aspirate),
                    target.well,
                    None,
                )
        if isinstance(target, validation.PointTarget):
            return target.location, None, None

    def _handle_dispense_target(
        self, target: Union[validation.WellTarget, validation.PointTarget]
    ) -> tuple[
        types.Location, Optional[labware.Well], Optional[types.MeniscusTrackingTarget]
    ]:
        if isinstance(target, validation.WellTarget):
            if target.location:
                return target.location, target.well, target.location.meniscus_tracking
            elif target.well.parent._core.is_fixed_trash():
                return target.well.top(), target.well, None
            else:
                return (
                    target.well.bottom(z=self._well_bottom_clearances.dispense),
                    target.well,
                    None,
                )
        if isinstance(target, validation.PointTarget):
            return target.location, None, None


class AutoProbeDisable:
    """Use this class to temporarily disable automatic liquid presence detection."""

    def __init__(self, instrument: InstrumentContext):
        self.instrument = instrument

    def __enter__(self) -> None:
        if self.instrument.api_version >= APIVersion(2, 21):
            self.auto_presence = self.instrument.liquid_presence_detection
            self.instrument.liquid_presence_detection = False

    def __exit__(self, *args: Any, **kwargs: Any) -> None:
        if self.instrument.api_version >= APIVersion(2, 21):
            self.instrument.liquid_presence_detection = self.auto_presence


def _raise_if_has_end_or_front_right_or_back_left(
    style: NozzleLayout,
    end: Optional[str],
    front_right: Optional[str],
    back_left: Optional[str],
) -> None:
    if any([end, front_right, back_left]):
        raise ValueError(
            f"Parameters 'end', 'front_right' and 'back_left' cannot be used with "
            f"the {style.name} nozzle configuration."
        )


def _check_valid_start_nozzle(style: NozzleLayout, start: Optional[str]) -> str:
    if start is None:
        raise ValueError(
            f"Cannot configure a nozzle layout of style {style.value} without a starting nozzle."
        )
    if start not in types.ALLOWED_PRIMARY_NOZZLES:
        raise ValueError(
            f"Starting nozzle specified is not one of {types.ALLOWED_PRIMARY_NOZZLES}."
        )
    return start


def _check_valid_end_nozzle(start: str, end: Optional[str]) -> str:
    if end is None:
        raise ValueError("Partial column configurations require the 'end' parameter.")
    if start[0] in end:
        raise ValueError(
            "The 'start' and 'end' parameters of a partial column configuration cannot be in the same row."
        )
    if start == "H1" or start == "H12":
        if "A" in end:
            raise ValueError(
                f"A partial column configuration with 'start'={start} cannot have its 'end' parameter be in row A. Use `ALL` configuration to utilize all nozzles."
            )
    elif start == "A1" or start == "A12":
        if "H" in end:
            raise ValueError(
                f"A partial column configuration with 'start'={start} cannot have its 'end' parameter be in row H. Use `ALL` configuration to utilize all nozzles."
            )
    return end


def _raise_if_no_front_right_or_back_left_for_quadrant(
    front_right: Optional[str], back_left: Optional[str]
) -> None:
    if front_right is None and back_left is None:
        raise ValueError(
            "Cannot configure a QUADRANT layout without a front right or back left nozzle."
        )


def _raise_if_has_end_nozzle_for_quadrant(end: Optional[str]) -> None:
    if end is not None:
        raise ValueError(
            "Parameter 'end' is not supported for QUADRANT configuration."
            " Use 'front_right' and 'back_left' arguments to specify the quadrant nozzle map instead."
        )


def _raise_if_has_front_right_or_back_left_for_partial_column(
    front_right: Optional[str], back_left: Optional[str]
) -> None:
    if any([front_right, back_left]):
        raise ValueError(
            "Parameters 'front_right' and 'back_left' cannot be used with "
            "the PARTIAL_COLUMN configuration."
        )
