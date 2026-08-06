/**
 * Appointment lifecycle statuses.
 *
 * Pending    -> just created (via WhatsApp or dashboard), not yet confirmed
 * Confirmed  -> receptionist / patient confirmed the booking
 * Arrived    -> patient has checked in at the clinic
 * Completed  -> consultation finished
 * Cancelled  -> booking cancelled
 */
const APPOINTMENT_STATUS = Object.freeze({
    PENDING: "Pending",
    CONFIRMED: "Confirmed",
    ARRIVED: "Arrived",
    COMPLETED: "Completed",
    CANCELLED: "Cancelled",
});

const APPOINTMENT_STATUS_VALUES = Object.freeze(
    Object.values(APPOINTMENT_STATUS)
);

module.exports = { APPOINTMENT_STATUS, APPOINTMENT_STATUS_VALUES };
