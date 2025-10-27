PETSAFE_API_BASE = "https://platform.cloud.petsafe.net/"
PETSAFE_CLIENT_ID = "18hpp04puqmgf5nc6o474lcp2g"
PETSAFE_REGION = "us-east-1"

FOOD_LEVEL_FULL = 0
FOOD_LEVEL_LOW = 1
FOOD_LEVEL_EMPTY = 2
SMARTDOOR_MODE_MANUAL_LOCKED = "MANUAL_LOCKED"
SMARTDOOR_MODE_MANUAL_UNLOCKED = "MANUAL_UNLOCKED"
SMARTDOOR_MODE_SMART = "SMART"

# SmartDoor access values used for schedules and overrides. 0 locks the door,
# 1 allows pets to enter only, 2 allows exit only, and 3 permits free access in
# both directions.
SMARTDOOR_ACCESS_LOCKED = 0
SMARTDOOR_ACCESS_IN_ONLY = 1
SMARTDOOR_ACCESS_OUT_ONLY = 2
SMARTDOOR_ACCESS_UNLOCKED = 3

# SmartDoor "final act" values representing the action to perform when power is
# lost or restored.
SMARTDOOR_FINAL_ACT_LOCKED = "LOCKED"
SMARTDOOR_FINAL_ACT_UNLOCKED = "UNLOCKED"

# SmartDoor schedule types documented by the API. Currently only SMART is
# observed in the Postman collection but the constant is provided for
# convenience.
SMARTDOOR_SCHEDULE_TYPE_SMART = "SMART"
