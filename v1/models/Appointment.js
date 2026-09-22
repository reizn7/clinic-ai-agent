const mongoose = require("mongoose");
const {
    APPOINTMENT_STATUS,
    APPOINTMENT_STATUS_VALUES,
} = require("../constants");

const appointmentSchema = new mongoose.Schema(
    {
        patientId: {
            type: mongoose.Schema.Types.ObjectId,
            ref: "Patient",
            required: true,
            index: true,
        },
        doctorId: {
            type: mongoose.Schema.Types.ObjectId,
            ref: "Doctor",
            required: true,
            index: true,
        },
        // Stored as "YYYY-MM-DD" (see utils/dateUtils.js for the rationale).
        date: {
            type: String,
            required: true,
            index: true,
        },
        // Slot start time as "HH:mm".
        slot: {
            type: String,
            required: true,
        },
        status: {
            type: String,
            enum: APPOINTMENT_STATUS_VALUES,
            default: APPOINTMENT_STATUS.PENDING,
            index: true,
        },
    },
    { timestamps: true }
);

// Prevent double-booking the same doctor/date/slot for active appointments.
appointmentSchema.index(
    { doctorId: 1, date: 1, slot: 1 },
    {
        unique: true,
        partialFilterExpression: {
            status: {
                $nin: [APPOINTMENT_STATUS.CANCELLED],
            },
        },
    }
);

module.exports = mongoose.model("Appointment", appointmentSchema);
