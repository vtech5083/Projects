-- Query 1: Selects sales data and calculates the difference between monthly home sales
-- and the ten-year monthly home sales average as a derived attribute for records from January 1, 2023.
SELECT Date,
       MonthlyHomeSales,
       TenYears_Monthly_HomeSales_Average,
       (MonthlyHomeSales - TenYears_Monthly_HomeSales_Average) AS SalesDifference
FROM SalesData
WHERE Date >= '2023-01-01';

-- Query 2: Performs an inner join between SalesData and PriceData on their Date columns,
-- retrieving the date, monthly home sales, and average price in Canada.
SELECT sd.Date,
       sd.MonthlyHomeSales,
       pd.AveragePriceCanada
FROM SalesData sd
INNER JOIN PriceData pd ON sd.Date = pd.Date;

-- Query 3: Averages monthly home sales grouped by year, providing an annual overview of sales activity.
SELECT YEAR(Date) AS SaleYear,
       ROUND(AVG(MonthlyHomeSales), 0) AS AvgMonthlySales
FROM SalesData
GROUP BY SaleYear;

-- Query 4: Calculates the average sales difference across all entries in SalesData
-- by using a subquery to first derive the sales difference.
SELECT Round(AVG(SalesDifference), 0) AS AvgSalesDifference
FROM (
  SELECT (MonthlyHomeSales - TenYears_Monthly_HomeSales_Average) AS SalesDifference
  FROM SalesData
) AS SalesDifferences;


-- Drop the view if it already exists
DROP VIEW IF EXISTS SalesPriceView;

-- Query 5: Sequences Of queries
-- Step 1: Create a view named SalesPriceView. This view joins SalesData and PriceData
-- and includes a derived attribute, PricePerSale, which is the average price per sale.

CREATE VIEW SalesPriceView AS
SELECT sd.Date,
       sd.MonthlyHomeSales,
       pd.AveragePriceCanada,
       (pd.AveragePriceCanada / sd.MonthlyHomeSales) AS PricePerSale
FROM SalesData sd
JOIN PriceData pd ON sd.Date = pd.Date;

-- Step 2: Query the SalesPriceView to observe the current state of data through the view.
SELECT * FROM SalesPriceView;

-- Make sure to OFF the safe mode for exceuting Step 3
SET SQL_SAFE_UPDATES = 0;

-- Step 3: Update the AveragePriceCanada in PriceData by 5% for all records in 2023.
-- This simulates a data correction to the average prices.
UPDATE PriceData
SET AveragePriceCanada = AveragePriceCanada * 1.05
WHERE YEAR(Date) = 2023;

-- Step 4: Re-query the SalesPriceView to see how the view reflects the updates made
-- to the underlying PriceData table, including recalculated PricePerSale values.
SELECT * FROM SalesPriceView;

-- Make sure to ON the safe mode for exceuting Step 3
SET SQL_SAFE_UPDATES = 1;

