# WiFi credentials — copy to lib/secrets.py and fill in your own.
#
# List as many networks as you like. On boot the meter scans, then tries the
# networks it can actually see in the order given here, so put your preferred
# network first. Hidden SSIDs never show up in a scan — they are still tried,
# just after the visible ones.

WIFI_NETWORKS = [
    ("your_network_name",   "your_wifi_password"),
    ("phone_hotspot_name",  "your_hotspot_password"),
]
