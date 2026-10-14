"""公开路径与服务编号；可选服务默认关闭。"""
SERVICES = {
    "d": {
        "login": {
            "serviceId": "D-S01",
            "required": True
        },
        "get_identity": {
            "serviceId": "D-S02",
            "required": True
        },
        "logout": {
            "serviceId": "D-S03",
            "required": True
        },
        "check_permission": {
            "serviceId": "D-S04",
            "required": True
        },
        "manage_roles": {
            "serviceId": "D-S05",
            "required": True
        },
        "query_master": {
            "serviceId": "D-S06",
            "required": True
        },
        "maintain_master": {
            "serviceId": "D-S07",
            "required": True
        },
        "query_people": {
            "serviceId": "D-S08",
            "required": True
        },
        "send_notification": {
            "serviceId": "D-S09",
            "required": True
        },
        "get_notification": {
            "serviceId": "D-S10",
            "required": True
        },
        "audit": {
            "serviceId": "D-S11",
            "required": True
        },
        "manage_public_config": {
            "serviceId": "D-S12",
            "required": True
        }
    },
    "m1": {
        "get_config": {
            "serviceId": "M1-S01",
            "required": True
        },
        "update_config": {
            "serviceId": "M1-S01",
            "required": True
        },
        "manage_roles": {
            "serviceId": "M1-S02",
            "required": True
        },
        "query_master": {
            "serviceId": "M1-S02",
            "required": True
        },
        "maintain_master": {
            "serviceId": "M1-S02",
            "required": True
        },
        "get_opening_times": {
            "serviceId": "M1-S02",
            "required": True
        },
        "set_opening_times": {
            "serviceId": "M1-S02",
            "required": True
        },
        "search_seats": {
            "serviceId": "M1-S03",
            "required": True
        },
        "reserve_seat": {
            "serviceId": "M1-S04",
            "required": True
        },
        "get_check_in_code": {
            "serviceId": "M1-S05",
            "required": True
        },
        "check_in": {
            "serviceId": "M1-S06",
            "required": True
        },
        "get_my_reservations": {
            "serviceId": "M1-S07",
            "required": True
        },
        "get_my_reservation": {
            "serviceId": "M1-S07",
            "required": True
        },
        "get_usage": {
            "serviceId": "M1-S08",
            "required": True
        },
        "get_room_availability": {
            "serviceId": "M1-S09",
            "required": True
        },
        "change_room_limit": {
            "serviceId": "M1-S10",
            "required": True
        },
        "get_room_limit": {
            "serviceId": "M1-S11",
            "required": True
        }
    },
    "m2": {
        "get_allowed_actions": {
            "serviceId": "M2-S01",
            "required": True
        },
        "submit_application": {
            "serviceId": "M2-S02",
            "required": True
        },
        "get_approval_tasks": {
            "serviceId": "M2-S03",
            "required": True
        },
        "submit_approval": {
            "serviceId": "M2-S04",
            "required": True
        },
        "get_application": {
            "serviceId": "M2-S05",
            "required": True
        },
        "get_applications": {
            "serviceId": "M2-S06",
            "required": False
        },
        "cancel_application": {
            "serviceId": "M2-S07",
            "required": False
        },
        "get_approval_config": {
            "serviceId": "M2-S08",
            "required": True
        },
        "update_approval_config": {
            "serviceId": "M2-S08",
            "required": True
        },
        "get_maintenance_impact": {
            "serviceId": "M2-S09",
            "required": False
        },
        "get_classroom_options": {
            "serviceId": "M2-S10",
            "required": True
        }
    },
    "m3": {
        "report_fault": {
            "serviceId": "M3-S01",
            "required": True
        },
        "get_dispatch_tasks": {
            "serviceId": "M3-S02",
            "required": True
        },
        "dispatch_order": {
            "serviceId": "M3-S03",
            "required": True
        },
        "get_work_tasks": {
            "serviceId": "M3-S04",
            "required": True
        },
        "start_repair": {
            "serviceId": "M3-S05",
            "required": True
        },
        "submit_completion": {
            "serviceId": "M3-S06",
            "required": True
        },
        "submit_acceptance": {
            "serviceId": "M3-S07",
            "required": True
        },
        "submit_evaluation": {
            "serviceId": "M3-S08",
            "required": True
        },
        "get_work_orders": {
            "serviceId": "M3-S09",
            "required": False
        },
        "get_work_order": {
            "serviceId": "M3-S09",
            "required": False
        },
        "cancel_work_order": {
            "serviceId": "M3-S10",
            "required": False
        },
        "get_maintenance_stats": {
            "serviceId": "M3-S11",
            "required": False
        },
        "get_timeout_config": {
            "serviceId": "M3-S12",
            "required": True
        },
        "update_timeout_config": {
            "serviceId": "M3-S12",
            "required": True
        },
        "get_dispatch_candidates": {
            "serviceId": "M3-S13",
            "required": True
        },
        "get_report_locations": {
            "serviceId": "M3-S14",
            "required": True
        }
    }
}
