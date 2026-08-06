const ApiError = require("../utils/ApiError");

// Reached only when no route matched.
const notFound = (req, res, next) => {
    next(ApiError.notFound(`Route not found: ${req.method} ${req.originalUrl}`));
};

module.exports = notFound;
