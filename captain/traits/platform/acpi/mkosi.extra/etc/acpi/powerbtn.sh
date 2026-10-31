#!/bin/bash
# Handle power button press — graceful shutdown
logger -t acpid "Power button pressed, initiating shutdown"
/usr/bin/systemctl poweroff
