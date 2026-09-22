const asyncHandler = require("../utils/asyncHandler");
const { sendSuccess } = require("../utils/apiResponse");
const ApiError = require("../utils/ApiError");
const appointmentService = require("../services/appointmentService");

// GET /appointments?today=true&date=YYYY-MM-DD&status=&doctorId=&patientId=
const listAppointments = asyncHandler(async (req, res) => {
    const { today, date, status, doctorId, patientId } = req.query;
    const appointments = await appointmentService.list({
        today: today === "true",
        date,
        status,
        doctorId,
        patientId,
    });
    sendSuccess(res, appointments, "Appointments fetched");
});

// POST /appointments  (manual booking from the dashboard)
const createAppointment = asyncHandler(async (req, res) => {
    const { patientId, doctorId, date, slot, status } = req.body;
    if (!patientId || !doctorId || !date || !slot) {
        throw ApiError.badRequest(
            "patientId, doctorId, date and slot are required"
        );
    }
    const appointment = await appointmentService.create({
        patientId,
        doctorId,
        date,
        slot,
        status,
    });
    sendSuccess(res, appointment, "Appointment created", 201);
});

// PATCH /appointments/:id  (status transitions: Arrived / Completed / Cancelled...)
const updateAppointment = asyncHandler(async (req, res) => {
    const { status } = req.body;
    if (!status) throw ApiError.badRequest("status is required");
    const appointment = await appointmentService.updateStatus(
        req.params.id,
        status
    );
    sendSuccess(res, appointment, "Appointment updated");
});

module.exports = {
    listAppointments,
    createAppointment,
    updateAppointment,
};
