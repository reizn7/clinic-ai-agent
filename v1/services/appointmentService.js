const Appointment = require("../models/Appointment");
const Doctor = require("../models/Doctor");
const ApiError = require("../utils/ApiError");
const {
    APPOINTMENT_STATUS,
    APPOINTMENT_STATUS_VALUES,
} = require("../constants");
const { generateSlots, availableSlots } = require("../utils/slotGenerator");
const { todayKey } = require("../utils/dateUtils");

/**
 * Appointment data access + business logic (slot availability,
 * double-booking protection, status transitions).
 */

const ACTIVE_STATUSES = APPOINTMENT_STATUS_VALUES.filter(
    (s) => s !== APPOINTMENT_STATUS.CANCELLED
);

/** Slot start-times already taken for a doctor on a given date. */
const getBookedSlots = async (doctorId, date) => {
    const appts = await Appointment.find({
        doctorId,
        date,
        status: { $in: ACTIVE_STATUSES },
    }).select("slot -_id");
    return appts.map((a) => a.slot);
};

/** Free slots for a doctor on a date, derived from working hours minus booked. */
const getAvailableSlotsForDoctor = async (doctor, date) => {
    const all = generateSlots(doctor.workingHours, doctor.consultationDuration);
    const booked = await getBookedSlots(doctor._id, date);
    return availableSlots(all, booked);
};

/**
 * Create an appointment after re-checking slot availability. The unique
 * partial index on the model is the ultimate guard; this gives a clean
 * error message before hitting it.
 */
const create = async ({ patientId, doctorId, date, slot, status }) => {
    const doctor = await Doctor.findById(doctorId);
    if (!doctor) throw ApiError.notFound("Doctor not found");

    const booked = await getBookedSlots(doctorId, date);
    if (booked.includes(slot)) {
        throw ApiError.conflict("That slot has just been booked. Please pick another.");
    }

    try {
        return await Appointment.create({
            patientId,
            doctorId,
            date,
            slot,
            status: status || APPOINTMENT_STATUS.PENDING,
        });
    } catch (err) {
        // Duplicate key from the unique index => concurrent booking.
        if (err && err.code === 11000) {
            throw ApiError.conflict(
                "That slot has just been booked. Please pick another."
            );
        }
        throw err;
    }
};

/**
 * List appointments with optional filters. `today: true` restricts to
 * today's date. Results are populated for dashboard consumption.
 */
const list = async ({ date, today, status, doctorId, patientId } = {}) => {
    const query = {};
    if (today) query.date = todayKey();
    else if (date) query.date = date;
    if (status) query.status = status;
    if (doctorId) query.doctorId = doctorId;
    if (patientId) query.patientId = patientId;

    return Appointment.find(query)
        .populate("patientId", "name phone age gender")
        .populate("doctorId", "name specialization")
        .sort({ date: 1, slot: 1 });
};

const findById = async (id) =>
    Appointment.findById(id)
        .populate("patientId", "name phone age gender")
        .populate("doctorId", "name specialization");

/** Update the status of an appointment (Arrived / Completed / Cancelled...). */
const updateStatus = async (id, status) => {
    if (!APPOINTMENT_STATUS_VALUES.includes(status)) {
        throw ApiError.badRequest(
            `Invalid status. Allowed: ${APPOINTMENT_STATUS_VALUES.join(", ")}`
        );
    }
    const appt = await Appointment.findByIdAndUpdate(
        id,
        { status },
        { returnDocument: "after", runValidators: true }
    )
        .populate("patientId", "name phone age gender")
        .populate("doctorId", "name specialization");

    if (!appt) throw ApiError.notFound("Appointment not found");
    return appt;
};

module.exports = {
    getBookedSlots,
    getAvailableSlotsForDoctor,
    create,
    list,
    findById,
    updateStatus,
};
