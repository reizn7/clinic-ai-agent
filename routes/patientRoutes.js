const express = require("express");
const { listPatients } = require("../controllers/patientController");

const router = express.Router();

router.get("/", listPatients);

module.exports = router;
