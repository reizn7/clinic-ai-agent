const asyncHandler = require("../utils/asyncHandler");
const { sendSuccess } = require("../utils/apiResponse");
const patientService = require("../services/patientService");

// GET /patients?search=<name|phone>
const listPatients = asyncHandler(async (req, res) => {
    const patients = await patientService.list({ search: req.query.search });
    sendSuccess(res, patients, "Patients fetched");
});

module.exports = { listPatients };
