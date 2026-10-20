-- Fictional identities; balances start at zero. Funding is explicitly simulated through the API.
INSERT INTO accounts(id,owner_name,kind,normal_side) VALUES
 ('alice','Alice Student','STUDENT','CREDIT'),
 ('bob','Bob Student','STUDENT','CREDIT'),
 ('cafe','Campus Cafe','MERCHANT','CREDIT'),
 ('books','University Bookshop','MERCHANT','CREDIT'),
 ('funding','Simulated funding clearing','FUNDING','DEBIT'),
 ('settled','Simulated bank settlement clearing','SETTLED','CREDIT');
