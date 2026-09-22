require("dotenv").config();

const express = require("express");

const connectDB = require("./config/db");
const webhookRoutes = require("./routes/webhook");
const apiRoutes = require("./routes");
const notFound = require("./middlewares/notFound");
const errorHandler = require("./middlewares/errorHandler");

const app = express();

app.use(express.json());

// Health check.
app.get("/", (req, res) => {
    res.send("Clinic AI Agent Running");
});

// WhatsApp webhook (verification + incoming messages) — unchanged mount.
app.use("/webhook", webhookRoutes);

// Dashboard-facing REST API.
app.use("/", apiRoutes);

// 404 + centralized error handling (must be registered last).
app.use(notFound);
app.use(errorHandler);

const PORT = process.env.PORT || 3000;

/**
 * Bootstrap: connect to MongoDB BEFORE Express starts accepting traffic,
 * so no request is ever served without a database connection.
 */
const start = async () => {
    await connectDB();
    app.listen(PORT, () => {
        console.log(`Server running on port ${PORT}`);
    });
};

start();
