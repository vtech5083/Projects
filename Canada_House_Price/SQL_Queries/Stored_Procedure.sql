
-- 1. Procedure to update the existing sales data
DELIMITER $$

DROP PROCEDURE IF EXISTS UpdateRecordSalesData$$

CREATE PROCEDURE UpdateRecordSalesData(
    IN saleDate DATE,
    IN monthlySales INT, 
    IN tenYearAvg INT
)
BEGIN
    DECLARE dateexists INT DEFAULT 0;
    DECLARE CONTINUE HANDLER FOR SQLEXCEPTION
    BEGIN
        -- Rollback the transaction in case of an error
        ROLLBACK;
    END;

    -- Start the transaction
    START TRANSACTION;
    
    SELECT COUNT(*) INTO dateexists FROM SalesData WHERE Date = saleDate;

    IF dateexists = 0 THEN
        INSERT INTO SalesData (Date, MonthlyHomeSales, TenYears_Monthly_HomeSales_Average) 
        VALUES (saleDate, monthlySales, tenYearAvg);
    ELSE
        UPDATE SalesData 
        SET MonthlyHomeSales = monthlySales, TenYears_Monthly_HomeSales_Average = tenYearAvg 
        WHERE Date = saleDate;
    END IF;
    
    -- Commit the transaction
    COMMIT;
END$$

DELIMITER ;

-- Call Statement
CALL UpdateRecordSalesData('2023-01-01', 33466, 43061);


-- 2. Procedure to add a new SeasonallyAdjusted record for a new date and then updating the PriceData table to reflect this new entry
DELIMITER $$

DROP PROCEDURE IF EXISTS InsertAndUpdateSeasonalAndPriceData$$

CREATE PROCEDURE InsertAndUpdateSeasonalAndPriceData(
    IN inDate DATE,
    IN inCompositeHPI_SA DECIMAL(10,2),
    IN inAveragePriceCanada INT,
    IN inAggregateCompositeCanada INT
)
BEGIN
    DECLARE entryExists INT;

    START TRANSACTION;
    
    -- Attempt to insert into SeasonallyAdjusted. If entry for date exists, it will silently fail due to the PRIMARY KEY constraint.
    INSERT IGNORE INTO SeasonallyAdjusted (Date, Composite_HPI_SA)
    VALUES (inDate, inCompositeHPI_SA);
    
    -- Check if a PriceData entry exists for the given date
    SELECT COUNT(*) 
		INTO entryExists 
        FROM PriceData 
        WHERE Date = inDate;

    IF entryExists > 0 THEN
        -- Update PriceData if an entry for the date exists
        UPDATE PriceData
        SET AveragePriceCanada = inAveragePriceCanada, AggregateCompositeCanada = inAggregateCompositeCanada
        WHERE Date = inDate;
    ELSE
        -- Insert into PriceData if no entry exists for the date
        INSERT INTO PriceData (Date, AveragePriceCanada, AggregateCompositeCanada)
        VALUES (inDate, inAveragePriceCanada, inAggregateCompositeCanada);
    END IF;
    
    COMMIT;
END$$

DELIMITER ;
 -- Cal statement to insert new value and update table
CALL InsertAndUpdateSeasonalAndPriceData('2024-01-01', 130.75, 600000, 650000);
