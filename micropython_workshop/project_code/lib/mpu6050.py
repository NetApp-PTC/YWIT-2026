"""
MPU-6050 6-Axis Accelerometer and Gyroscope Driver for MicroPython.

Reads 3-axis accelerometer and 3-axis gyroscope data over I2C.
"""

import struct


class MPU6050:
    def __init__(self, i2c, addr=0x68):
        self.i2c = i2c
        self.addr = addr
        self.wake()

    def wake(self):
        """Wake up the MPU-6050 by clearing the sleep bit in PWR_MGMT_1."""
        self.i2c.writeto_mem(self.addr, 0x6B, b"\x00")

    def who_am_i(self):
        """Return the device ID from register 0x75 (typically 0x68)."""
        return self.i2c.readfrom_mem(self.addr, 0x75, 1)[0]

    def read_accel_raw(self):
        """Read raw signed 16-bit integers for acceleration (X, Y, Z)."""
        data = self.i2c.readfrom_mem(self.addr, 0x3B, 6)
        return struct.unpack(">hhh", data)

    def read_accel(self):
        """Read acceleration in g units (±2g full-scale range)."""
        ax, ay, az = self.read_accel_raw()
        return ax / 16384.0, ay / 16384.0, az / 16384.0

    def read_gyro_raw(self):
        """Read raw signed 16-bit integers for angular velocity (X, Y, Z)."""
        data = self.i2c.readfrom_mem(self.addr, 0x43, 6)
        return struct.unpack(">hhh", data)

    def read_gyro(self):
        """Read angular velocity in deg/s (±250 deg/s full-scale range)."""
        gx, gy, gz = self.read_gyro_raw()
        return gx / 131.0, gy / 131.0, gz / 131.0
