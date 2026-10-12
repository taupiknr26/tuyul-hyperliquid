-- One query from any fill back to its order, trade, risk assessment,
-- signal and the raw Telegram message (FR-090, OBJ-7).
CREATE VIEW fill_trace AS
SELECT
    f.tid               AS fill_tid,
    f.time              AS fill_time,
    f.coin              AS coin,
    f.side              AS fill_side,
    f.price             AS fill_price,
    f.size              AS fill_size,
    f.fee               AS fill_fee,
    f.closed_pnl        AS fill_closed_pnl,
    f.liquidation       AS fill_liquidation,
    o.id                AS order_id,
    o.role              AS order_role,
    o.leg               AS order_leg,
    o.cloid             AS order_cloid,
    o.oid               AS order_oid,
    o.state             AS order_state,
    t.id                AS trade_id,
    t.state             AS trade_state,
    t.outcome           AS trade_outcome,
    t.net_pnl           AS trade_net_pnl,
    t.config_version    AS config_version,
    r.id                AS risk_assessment_id,
    r.outputs           AS risk_outputs,
    s.id                AS signal_id,
    s.content_id        AS signal_content_id,
    s.state             AS signal_state,
    m.channel_id        AS channel_id,
    m.message_id        AS message_id,
    m.channel_ref       AS channel_ref,
    m.sent_at           AS message_sent_at,
    m.received_at       AS message_received_at,
    m.text              AS message_text
FROM fills f
LEFT JOIN orders o ON o.id = f.order_id
LEFT JOIN trades t ON t.id = f.trade_id
LEFT JOIN risk_assessments r ON r.id = t.risk_assessment_id
LEFT JOIN signals s ON s.id = t.signal_id
LEFT JOIN messages m ON m.channel_id = s.channel_id AND m.message_id = s.message_id;
