const asyncHandler = require("../utils/asyncHandler");
const { sendSuccess } = require("../utils/apiResponse");
const appointmentService = require("../services/appointmentService");
const { APPOINTMENT_STATUS_VALUES } = require("../constants");
const { todayKey } = require("../utils/dateUtils");

/**
 * Dashboard read-model endpoints for the future receptionist UI.
 * All mutations (Mark Arrived / Completed / Cancel) reuse
 * PATCH /appointments/:id — no duplicate write logic here.
 */

// GET /dashboard/today  -> today's appointments + a status breakdown
const today = asyncHandler(async (req, res) => {
    const appointments = await appointmentService.list({ today: true });

    const counts = APPOINTMENT_STATUS_VALUES.reduce((acc, s) => {
        acc[s] = 0;
        return acc;
    }, {});
    for (const appt of appointments) counts[appt.status] += 1;

    sendSuccess(
        res,
        { date: todayKey(), total: appointments.length, counts, appointments },
        "Today's appointments"
    );
});

module.exports = { today };
