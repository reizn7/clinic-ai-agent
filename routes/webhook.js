const express = require("express");

const conversationService = require("../services/conversationService");
const whatsappService = require("../services/whatsappService");

const router = express.Router();

/*
Webhook Verification
(unchanged — verification logic is identical to the working version)
*/

router.get("/", (req, res) => {
    const mode = req.query["hub.mode"];
    const token = req.query["hub.verify_token"];
    const challenge = req.query["hub.challenge"];

    if (
        mode === "subscribe" &&
        token === process.env.VERIFY_TOKEN
    ) {
        console.log("Webhook Verified!");
        return res.status(200).send(challenge);
    }

    return res.sendStatus(403);
});

/*
Incoming Messages

The webhook now simply:
  1. extracts the incoming message (same parsing as before),
  2. asks the conversation engine for a reply,
  3. sends it via the WhatsApp service.
No Axios / Graph API code lives here anymore — sending moved to
services/whatsappService.js with the exact same request it used before.
*/

router.post("/", async (req, res) => {
    try {
        console.log(JSON.stringify(req.body, null, 2));

        const message =
            req.body.entry?.[0]?.changes?.[0]?.value?.messages?.[0];

        // No user message (e.g. delivery/read status callbacks) -> ack.
        if (!message) {
            return res.sendStatus(200);
        }

        const from = message.from;
        const text = message.text?.body || "";

        console.log("Message Received:", text);
        console.log("From:", from);

        // Rule-based engine decides the reply and updates conversation state.
        const reply = await conversationService.handleMessage(from, text);

        // Deliver it over WhatsApp Cloud API.
        await whatsappService.sendTextMessage(from, reply);

        console.log("Reply Sent!");

        return res.sendStatus(200);
    } catch (err) {
        console.error("Error:", err.response?.data || err.message);
        return res.sendStatus(500);
    }
});

module.exports = router;
