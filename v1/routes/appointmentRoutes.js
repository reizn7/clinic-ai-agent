const express = require("express");
const {
    listAppointments,
    createAppointment,
    updateAppointment,
} = require("../controllers/appointmentController");

const router = express.Router();

router.get("/", listAppointments);
router.post("/", createAppointment);
router.patch("/:id", updateAppointment);

module.exports = router;
