-- What a request's disposal is compared with other disposals by: the vehicle number (cleaned up; NULL when none
-- could be), the quantity entered (kg as check_proof reads it, or the number and unit) and the trip's date in IST.
ALTER TABLE verification_request ADD COLUMN vehicle_number text;
ALTER TABLE verification_request ADD COLUMN quantity text;
ALTER TABLE verification_request ADD COLUMN trip_day date;
CREATE INDEX verification_request_vehicle_day ON verification_request (vehicle_number, trip_day);
