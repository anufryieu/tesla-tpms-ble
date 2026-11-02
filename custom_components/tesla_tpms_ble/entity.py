"""Shared behaviour for every Tesla / Autel BLE TPMS entity."""
from __future__ import annotations

from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothProcessorEntity,
)


class TeslaTPMSEntity(PassiveBluetoothProcessorEntity):
    """Base entity: stays available once seen, and admits when it is stale.

    The base class ties availability to the processor, which is right for a
    device that is always on. A TPMS sensor sleeps when the wheel is not
    turning, so going quiet is its normal state and must not mark the entity
    unavailable -- that would drop the last known pressure every time the car
    is parked, which is exactly when a dashboard is read. The reading is
    instead flagged ``assumed_state`` once the sensor stops broadcasting.

    ``assumed_state`` is not enough on its own, which is why the age of the
    reading is published as a ``Last reading`` entity of its own: the flag
    reaches the state attributes, but Home Assistant only draws it for things
    you can operate, so nothing about it shows on a sensor's card.

    ``Awake`` is the one value no arriving message can clear -- a sleeping
    sensor sends nothing to say it has stopped -- so it is told from the
    integration's own clock instead; see
    :meth:`~.parser.TeslaTPMSBluetoothDeviceData.update_quiet`.
    """

    @property
    def available(self) -> bool:
        """Return True once the sensor has ever been seen."""
        return True

    @property
    def assumed_state(self) -> bool:
        """Return True when the sensor is no longer broadcasting."""
        return not self.processor.available
