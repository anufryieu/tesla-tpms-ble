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
    """

    @property
    def available(self) -> bool:
        """Return True once the sensor has ever been seen."""
        return True

    @property
    def assumed_state(self) -> bool:
        """Return True when the sensor is no longer broadcasting."""
        return not self.processor.available
