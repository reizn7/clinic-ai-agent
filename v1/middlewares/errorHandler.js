const ApiError = require("../utils/ApiError");

/**
 * Central Express error handler. Turns thrown errors into a consistent
 * JSON envelope. Must be registered LAST (after all routes).
 */
// eslint-disable-next-line no-unused-vars
const errorHandler = (err, req, res, next) => {
    let statusCode = err.statusCode || 500;
    let message = err.message || "Internal Server Error";
    let details = err.details;

    // Mongoose validation -> 400
    if (err.name === "ValidationError") {
        statusCode = 400;
        message = "Validation failed";
        details = Object.values(err.errors).map((e) => e.message);
    }

    // Bad ObjectId -> 400
    if (err.name === "CastError") {
        statusCode = 400;
        message = `Invalid ${err.path}: ${err.value}`;
    }

    // Duplicate key -> 409
    if (err.code === 11000) {
        statusCode = 409;
        message = "Duplicate value";
        details = err.keyValue;
    }

    if (statusCode >= 500) {
        console.error("Unhandled error:", err);
    }

    res.status(statusCode).json({
        success: false,
        message,
        ...(details !== undefined ? { details } : {}),
    });
};

module.exports = errorHandler;
