SELECT
    customer_id,
    MAX(customer_name) AS name,
    COUNT(*) AS order_count,
    SUM(amount) AS total_amount
FROM orders
WHERE status = 'paid'
GROUP BY customer_id
