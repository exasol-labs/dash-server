# Schema Scaffold Notes for MART.ORDER_LINES

- Summary heading: `Line KPI Snapshot`
- Chart heading: `Line Trend`
- Table heading: `Line Detail`
- Time column: `none`
- Dimension column: `ORDER_ID`
- Measure columns: `QUANTITY, NET_UNIT_PRICE, UNIT_COST`
- Relationship hints:
  - `ORDER_ID` also appears in `MART.ORDERS`
