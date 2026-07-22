-- Replace this with a detail table query from the target business schema.
SELECT 'North' AS SEGMENT, 410 AS ORDERS, 18.1 AS AVG_ORDER_VALUE FROM DUAL
UNION ALL SELECT 'South', 355, 17.9 FROM DUAL
UNION ALL SELECT 'East', 289, 19.2 FROM DUAL
UNION ALL SELECT 'West', 186, 20.1 FROM DUAL
