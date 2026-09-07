"""Sensor platform for Tesla / Autel BLE TPMS."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothProcessorCoordinator,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info
from sensor_state_data import SensorUpdate

from .const import DOMAIN
from .device import device_key_to_bluetooth_entity_key
from .parser import TPMSSensor

SENSOR_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    TPMSSensor.PRESSURE: SensorEntityDescription(
        key=TPMSSensor.PRESSURE,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.BAR,
        suggested_display_precision=2,
    ),
    TPMSSensor.TEMPERATURE: SensorEntityDescription(
        key=TPMSSensor.TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    TPMSSensor.BATTERY: SensorEntityDescription(
        key=TPMSSensor.BATTERY,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
    ),
    TPMSSensor.VOLTAGE: SensorEntityDescription(
        key=TPMSSensor.VOLTAGE,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    TPMSSensor.SIGNAL_STRENGTH: SensorEntityDescription(
        key=TPMSSensor.SIGNAL_STRENGTH,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # Diagnostics. These are the untouched wire values; they exist so the
    # decode can be checked against a reference gauge. See docs/CALIBRATION.md.
    TPMSSensor.RAW_PRESSURE: SensorEntityDescription(
        key=TPMSSensor.RAW_PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:numeric",
        entity_registry_enabled_default=False,
    ),
    TPMSSensor.RAW_TEMPERATURE: SensorEntityDescription(
        key=TPMSSensor.RAW_TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:numeric",
        entity_registry_enabled_default=False,
    ),
    TPMSSensor.STATUS: SensorEntityDescription(
        key=TPMSSensor.STATUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:information-outline",
        entity_registry_enabled_default=False,
    ),
}


def sensor_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate:
    """Convert a sensor update to a Bluetooth data update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: sensor_device_info_to_hass_device_info(device_info)
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(device_key): SENSOR_DESCRIPTIONS[
                device_key.key
            ]
            for device_key in sensor_update.entity_descriptions
            if device_key.key in SENSOR_DESCRIPTIONS
        },
        entity_data={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.entity_values.items()
        },
        entity_names={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.name
            for device_key, sensor_values in sensor_update.entity_values.items()
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the TPMS sensors."""
    coordinator: PassiveBluetoothProcessorCoordinator = hass.data[DOMAIN][
        entry.entry_id
    ]
    processor = PassiveBluetoothDataProcessor(sensor_update_to_bluetooth_data_update)
    entry.async_on_unload(
        processor.async_add_entities_listener(
            TeslaTPMSSensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, SensorEntityDescription)
    )


class TeslaTPMSSensorEntity(PassiveBluetoothProcessorEntity, SensorEntity):
    """A single value from a Tesla-protocol TPMS sensor."""

    @property
    def native_value(self) -> str | int | float | None:
        """Return the native value."""
        return self.processor.entity_data.get(self.entity_key)

    @property
    def available(self) -> bool:
        """Return True once the sensor has ever been seen.

        TPMS sensors sleep when the wheel is not turning, so going quiet is
        normal and must not mark the entity unavailable -- that would lose the
        last known pressure every time the car is parked.
        """
        return True

    @property
    def assumed_state(self) -> bool:
        """Return True when the sensor is no longer broadcasting."""
        return not self.processor.available
