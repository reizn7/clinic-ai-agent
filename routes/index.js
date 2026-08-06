const express = require("express");

const appointmentRoutes = require("./appointmentRoutes");
const patientRoutes = require("./patientRoutes");
const doctorRoutes = require("./doctorRoutes");
const dashboardRoutes = require("./dashboardRoutes");

/**
 * Aggregates all REST (dashboard-facing) routes. The WhatsApp webhook is
 * mounted separately in server.js so its flow stays isolated.
 */
const router = express.Router();

router.use("/appointments", appointmentRoutes);
router.use("/patients", patientRoutes);
router.use("/doctors", doctorRoutes);
router.use("/dashboard", dashboardRoutes);

module.exports = router;
