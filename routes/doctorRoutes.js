const express = require("express");
const {
    listDoctors,
    createDoctor,
    updateDoctor,
} = require("../controllers/doctorController");

const router = express.Router();

router.get("/", listDoctors);
router.post("/", createDoctor);
router.patch("/:id", updateDoctor);

module.exports = router;
