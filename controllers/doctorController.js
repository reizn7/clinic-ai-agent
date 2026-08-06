const asyncHandler = require("../utils/asyncHandler");
const { sendSuccess } = require("../utils/apiResponse");
const doctorService = require("../services/doctorService");

// GET /doctors?activeOnly=true
const listDoctors = asyncHandler(async (req, res) => {
    const activeOnly = req.query.activeOnly === "true";
    const doctors = await doctorService.list({ activeOnly });
    sendSuccess(res, doctors, "Doctors fetched");
});

// POST /doctors
const createDoctor = asyncHandler(async (req, res) => {
    const doctor = await doctorService.create(req.body);
    sendSuccess(res, doctor, "Doctor created", 201);
});

// PATCH /doctors/:id
const updateDoctor = asyncHandler(async (req, res) => {
    const doctor = await doctorService.update(req.params.id, req.body);
    sendSuccess(res, doctor, "Doctor updated");
});

module.exports = { listDoctors, createDoctor, updateDoctor };
