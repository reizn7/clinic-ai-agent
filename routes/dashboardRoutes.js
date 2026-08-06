const express = require("express");
const { today } = require("../controllers/dashboardController");

const router = express.Router();

router.get("/today", today);

module.exports = router;
