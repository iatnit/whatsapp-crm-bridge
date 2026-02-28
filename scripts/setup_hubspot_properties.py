"""One-time script: Create custom HubSpot contact properties for LOCA CRM.

Usage (on server):
    cd /opt/whatsapp-crm-bridge
    python -m scripts.setup_hubspot_properties

Or standalone:
    HUBSPOT_ACCESS_TOKEN=pat-xxx python scripts/setup_hubspot_properties.py
"""

import os
import sys
import json
import httpx
import time

BASE_URL = "https://api.hubapi.com"

# Try loading from .env first, then env var
TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
if not TOKEN:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    except ImportError:
        pass

if not TOKEN:
    print("ERROR: HUBSPOT_ACCESS_TOKEN not set. Provide via env var or .env file.")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

# ── Property Groups ──────────────────────────────────────────────────

GROUPS = [
    {
        "name": "loca_profile",
        "label": "LOCA - Customer Profile",
        "displayOrder": 1,
    },
    {
        "name": "loca_geography",
        "label": "LOCA - Geography",
        "displayOrder": 2,
    },
    {
        "name": "loca_product",
        "label": "LOCA - Product Interest",
        "displayOrder": 3,
    },
    {
        "name": "loca_competitive",
        "label": "LOCA - Competitive Intel",
        "displayOrder": 4,
    },
    {
        "name": "loca_sales",
        "label": "LOCA - Sales Process",
        "displayOrder": 5,
    },
    {
        "name": "loca_engagement",
        "label": "LOCA - Engagement & Tags",
        "displayOrder": 6,
    },
]

# ── Properties ────────────────────────────────────────────────────────

PROPERTIES = [
    # ── Customer Profile ──
    {
        "name": "customer_tier",
        "label": "Customer Tier",
        "groupName": "loca_profile",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Customer grade based on order volume and potential",
        "options": [
            {"label": "A - Key Account", "value": "A", "displayOrder": 1},
            {"label": "B - Medium", "value": "B", "displayOrder": 2},
            {"label": "C - Small", "value": "C", "displayOrder": 3},
            {"label": "D - Unqualified", "value": "D", "displayOrder": 4},
        ],
    },
    {
        "name": "customer_type",
        "label": "Customer Type",
        "groupName": "loca_profile",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Business type of the customer",
        "options": [
            {"label": "Manufacturer", "value": "manufacturer", "displayOrder": 1},
            {"label": "Wholesaler", "value": "wholesaler", "displayOrder": 2},
            {"label": "Retailer", "value": "retailer", "displayOrder": 3},
            {"label": "Agent / Distributor", "value": "agent", "displayOrder": 4},
            {"label": "Brand Owner", "value": "brand", "displayOrder": 5},
        ],
    },
    {
        "name": "moq_qualified",
        "label": "MOQ Qualified",
        "groupName": "loca_profile",
        "type": "enumeration",
        "fieldType": "booleancheckbox",
        "description": "Whether the customer meets MOQ threshold (50,000m+ or 50ctns+)",
        "options": [
            {"label": "Yes", "value": "true", "displayOrder": 1},
            {"label": "No", "value": "false", "displayOrder": 2},
        ],
    },
    {
        "name": "industry",
        "label": "Industry / Application",
        "groupName": "loca_profile",
        "type": "enumeration",
        "fieldType": "checkbox",
        "description": "Industry sectors the customer operates in",
        "options": [
            {"label": "Garment (服装)", "value": "garment", "displayOrder": 1},
            {"label": "Shoes (鞋材)", "value": "shoes", "displayOrder": 2},
            {"label": "Bags (箱包)", "value": "bags", "displayOrder": 3},
            {"label": "Accessories (饰品)", "value": "accessories", "displayOrder": 4},
            {"label": "Crafts (工艺品)", "value": "crafts", "displayOrder": 5},
            {"label": "Bridal (婚纱)", "value": "bridal", "displayOrder": 6},
            {"label": "Home Textile (家纺)", "value": "home_textile", "displayOrder": 7},
        ],
    },
    # ── Geography ──
    {
        "name": "customer_city",
        "label": "City",
        "groupName": "loca_geography",
        "type": "string",
        "fieldType": "text",
        "description": "Customer city (e.g. Delhi, Mumbai, Surat)",
    },
    {
        "name": "customer_state",
        "label": "State / Province",
        "groupName": "loca_geography",
        "type": "string",
        "fieldType": "text",
        "description": "Customer state or province (e.g. Maharashtra, Gujarat)",
    },
    {
        "name": "market_region",
        "label": "Market Region",
        "groupName": "loca_geography",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Geographic market region",
        "options": [
            {"label": "South Asia", "value": "south_asia", "displayOrder": 1},
            {"label": "Southeast Asia", "value": "southeast_asia", "displayOrder": 2},
            {"label": "Middle East", "value": "middle_east", "displayOrder": 3},
            {"label": "Africa", "value": "africa", "displayOrder": 4},
            {"label": "Latin America", "value": "latin_america", "displayOrder": 5},
            {"label": "Europe", "value": "europe", "displayOrder": 6},
            {"label": "North America", "value": "north_america", "displayOrder": 7},
            {"label": "Other", "value": "other", "displayOrder": 8},
        ],
    },
    # ── Product Interest ──
    {
        "name": "product_interest",
        "label": "Product Interest",
        "groupName": "loca_product",
        "type": "enumeration",
        "fieldType": "checkbox",
        "description": "Product lines the customer is interested in",
        "options": [
            {"label": "DR - Rhinestone Strip (抽条)", "value": "DR", "displayOrder": 1},
            {"label": "DS - Mesh Sheet (胶网)", "value": "DS", "displayOrder": 2},
            {"label": "DT - Transfer Design (排图)", "value": "DT", "displayOrder": 3},
            {"label": "DF - Loose Stone (散钻)", "value": "DF", "displayOrder": 4},
            {"label": "PVC - Transfer (转印)", "value": "PVC", "displayOrder": 5},
            {"label": "MA - Hotfix Patch (烫片)", "value": "MA", "displayOrder": 6},
            {"label": "SP - Special (特殊品)", "value": "SP", "displayOrder": 7},
        ],
    },
    {
        "name": "preferred_sizes",
        "label": "Preferred Sizes",
        "groupName": "loca_product",
        "type": "string",
        "fieldType": "text",
        "description": "Commonly ordered stone sizes (e.g. SS6, SS10, SS16, SS20)",
    },
    {
        "name": "annual_volume",
        "label": "Annual Purchase Volume",
        "groupName": "loca_product",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Estimated annual purchase volume in USD",
        "options": [
            {"label": "< $5K", "value": "under_5k", "displayOrder": 1},
            {"label": "$5K - $20K", "value": "5k_20k", "displayOrder": 2},
            {"label": "$20K - $50K", "value": "20k_50k", "displayOrder": 3},
            {"label": "$50K - $100K", "value": "50k_100k", "displayOrder": 4},
            {"label": "$100K+", "value": "over_100k", "displayOrder": 5},
        ],
    },
    # ── Competitive Intel ──
    {
        "name": "competitor_using",
        "label": "Competitor Using",
        "groupName": "loca_competitive",
        "type": "enumeration",
        "fieldType": "checkbox",
        "description": "Competitors the customer is currently buying from",
        "options": [
            {"label": "Amy (义乌)", "value": "amy", "displayOrder": 1},
            {"label": "Coco (义乌)", "value": "coco", "displayOrder": 2},
            {"label": "Yang (广州)", "value": "yang", "displayOrder": 3},
            {"label": "Preciosa", "value": "preciosa", "displayOrder": 4},
            {"label": "Other", "value": "other", "displayOrder": 5},
        ],
    },
    {
        "name": "price_sensitivity",
        "label": "Price Sensitivity",
        "groupName": "loca_competitive",
        "type": "enumeration",
        "fieldType": "select",
        "description": "How price-driven the customer is",
        "options": [
            {"label": "High - Price Driven", "value": "high", "displayOrder": 1},
            {"label": "Medium - Value Seeker", "value": "medium", "displayOrder": 2},
            {"label": "Low - Quality First", "value": "low", "displayOrder": 3},
        ],
    },
    {
        "name": "competitor_notes",
        "label": "Competitor Notes",
        "groupName": "loca_competitive",
        "type": "string",
        "fieldType": "textarea",
        "description": "Free-text notes about competitive situation",
    },
    # ── Sales Process ──
    {
        "name": "lead_source_channel",
        "label": "Lead Source Channel",
        "groupName": "loca_sales",
        "type": "enumeration",
        "fieldType": "select",
        "description": "How the customer first contacted us",
        "options": [
            {"label": "WhatsApp", "value": "whatsapp", "displayOrder": 1},
            {"label": "Instagram", "value": "instagram", "displayOrder": 2},
            {"label": "Email", "value": "email", "displayOrder": 3},
            {"label": "Trade Show", "value": "trade_show", "displayOrder": 4},
            {"label": "Referral", "value": "referral", "displayOrder": 5},
            {"label": "Website", "value": "website", "displayOrder": 6},
            {"label": "Alibaba", "value": "alibaba", "displayOrder": 7},
        ],
    },
    {
        "name": "customer_stage",
        "label": "Customer Stage",
        "groupName": "loca_sales",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Current stage in the sales pipeline",
        "options": [
            {"label": "New Lead", "value": "new_lead", "displayOrder": 1},
            {"label": "Contacted", "value": "contacted", "displayOrder": 2},
            {"label": "Qualified", "value": "qualified", "displayOrder": 3},
            {"label": "Negotiating", "value": "negotiating", "displayOrder": 4},
            {"label": "Ordered", "value": "ordered", "displayOrder": 5},
            {"label": "Repeat Buyer", "value": "repeat_buyer", "displayOrder": 6},
            {"label": "Dormant", "value": "dormant", "displayOrder": 7},
            {"label": "Lost", "value": "lost", "displayOrder": 8},
        ],
    },
    {
        "name": "sample_status",
        "label": "Sample Status",
        "groupName": "loca_sales",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Status of sample request",
        "options": [
            {"label": "Not Requested", "value": "not_requested", "displayOrder": 1},
            {"label": "Preparing", "value": "preparing", "displayOrder": 2},
            {"label": "Shipped", "value": "shipped", "displayOrder": 3},
            {"label": "Received", "value": "received", "displayOrder": 4},
            {"label": "Approved", "value": "approved", "displayOrder": 5},
            {"label": "Rejected", "value": "rejected", "displayOrder": 6},
        ],
    },
    {
        "name": "payment_terms",
        "label": "Payment Terms",
        "groupName": "loca_sales",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Preferred payment method",
        "options": [
            {"label": "TT (Bank Transfer)", "value": "tt", "displayOrder": 1},
            {"label": "L/C (Letter of Credit)", "value": "lc", "displayOrder": 2},
            {"label": "PayPal", "value": "paypal", "displayOrder": 3},
            {"label": "Western Union", "value": "western_union", "displayOrder": 4},
            {"label": "Cash", "value": "cash", "displayOrder": 5},
        ],
    },
    {
        "name": "shipping_preference",
        "label": "Shipping Preference",
        "groupName": "loca_sales",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Preferred shipping method",
        "options": [
            {"label": "Sea (海运)", "value": "sea", "displayOrder": 1},
            {"label": "Air (空运)", "value": "air", "displayOrder": 2},
            {"label": "Express (快递)", "value": "express", "displayOrder": 3},
        ],
    },
    {
        "name": "assigned_agent",
        "label": "Assigned Agent",
        "groupName": "loca_sales",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Local agent assigned to this customer (for small customers)",
        "options": [
            {"label": "Direct Customer", "value": "direct", "displayOrder": 1},
            {"label": "Sunil - Delhi", "value": "sunil_delhi", "displayOrder": 2},
            {"label": "Tan Singh - Mumbai", "value": "tansingh_mumbai", "displayOrder": 3},
            {"label": "Prakash - Mumbai", "value": "prakash_mumbai", "displayOrder": 4},
        ],
    },
    # ── Engagement & Tags ──
    {
        "name": "comm_language",
        "label": "Communication Language",
        "groupName": "loca_engagement",
        "type": "enumeration",
        "fieldType": "select",
        "description": "Primary communication language",
        "options": [
            {"label": "English", "value": "english", "displayOrder": 1},
            {"label": "Hindi", "value": "hindi", "displayOrder": 2},
            {"label": "Arabic", "value": "arabic", "displayOrder": 3},
            {"label": "Spanish", "value": "spanish", "displayOrder": 4},
            {"label": "French", "value": "french", "displayOrder": 5},
            {"label": "Other", "value": "other", "displayOrder": 6},
        ],
    },
    {
        "name": "whatsapp_number",
        "label": "WhatsApp Number",
        "groupName": "loca_engagement",
        "type": "string",
        "fieldType": "phonenumber",
        "description": "WhatsApp contact number (may differ from main phone)",
    },
    {
        "name": "instagram_handle",
        "label": "Instagram Handle",
        "groupName": "loca_engagement",
        "type": "string",
        "fieldType": "text",
        "description": "Instagram username (@handle)",
    },
    {
        "name": "customer_tags",
        "label": "Customer Tags",
        "groupName": "loca_engagement",
        "type": "enumeration",
        "fieldType": "checkbox",
        "description": "Flexible tags for customer categorization",
        "options": [
            {"label": "Hot Lead 🔥", "value": "hot_lead", "displayOrder": 1},
            {"label": "VIP 💎", "value": "vip", "displayOrder": 2},
            {"label": "Repeat Buyer 🔄", "value": "repeat_buyer", "displayOrder": 3},
            {"label": "First Timer 🆕", "value": "first_timer", "displayOrder": 4},
            {"label": "Price Shopper 📉", "value": "price_shopper", "displayOrder": 5},
            {"label": "Risky ⚠️", "value": "risky", "displayOrder": 6},
            {"label": "Agent Potential 🤝", "value": "agent_potential", "displayOrder": 7},
        ],
    },
    {
        "name": "first_contact_date",
        "label": "First Contact Date",
        "groupName": "loca_engagement",
        "type": "date",
        "fieldType": "date",
        "description": "Date of first communication with customer",
    },
    {
        "name": "last_order_date",
        "label": "Last Order Date",
        "groupName": "loca_engagement",
        "type": "date",
        "fieldType": "date",
        "description": "Date of most recent order",
    },
]


def create_group(client: httpx.Client, group: dict) -> bool:
    """Create a property group. Returns True if created or already exists."""
    url = f"{BASE_URL}/crm/v3/properties/contacts/groups"
    resp = client.post(url, json=group, headers=HEADERS)

    if resp.status_code == 201:
        print(f"  ✅ Group '{group['label']}' created")
        return True
    elif resp.status_code == 409:
        print(f"  ⏭️  Group '{group['label']}' already exists")
        return True
    else:
        print(f"  ❌ Group '{group['label']}' failed: {resp.status_code} {resp.text}")
        return False


def create_property(client: httpx.Client, prop: dict) -> bool:
    """Create a contact property. Returns True if created or already exists."""
    url = f"{BASE_URL}/crm/v3/properties/contacts"
    resp = client.post(url, json=prop, headers=HEADERS)

    if resp.status_code == 201:
        print(f"  ✅ Property '{prop['label']}' ({prop['name']}) created")
        return True
    elif resp.status_code == 409:
        print(f"  ⏭️  Property '{prop['label']}' ({prop['name']}) already exists")
        return True
    else:
        print(f"  ❌ Property '{prop['label']}' ({prop['name']}) failed: {resp.status_code} {resp.text}")
        return False


def main():
    print("=" * 60)
    print("LOCA HubSpot CRM Property Setup")
    print("=" * 60)

    client = httpx.Client(timeout=15)

    # Verify token
    print("\n🔑 Verifying HubSpot access...")
    resp = client.get(
        f"{BASE_URL}/crm/v3/properties/contacts",
        headers=HEADERS,
        params={"limit": 1},
    )
    if resp.status_code != 200:
        print(f"❌ Token invalid or expired: {resp.status_code}")
        sys.exit(1)
    print("✅ Token verified\n")

    # Create groups
    print("📁 Creating property groups...")
    for group in GROUPS:
        create_group(client, group)
        time.sleep(0.2)

    # Create properties
    print(f"\n📝 Creating {len(PROPERTIES)} properties...")
    success = 0
    skip = 0
    fail = 0
    for prop in PROPERTIES:
        resp = client.post(
            f"{BASE_URL}/crm/v3/properties/contacts",
            json=prop,
            headers=HEADERS,
        )
        if resp.status_code == 201:
            print(f"  ✅ {prop['label']} ({prop['name']})")
            success += 1
        elif resp.status_code == 409:
            print(f"  ⏭️  {prop['label']} ({prop['name']}) — already exists")
            skip += 1
        else:
            print(f"  ❌ {prop['label']} ({prop['name']}) — {resp.status_code}: {resp.text[:100]}")
            fail += 1
        time.sleep(0.2)  # Rate limiting

    # Summary
    print(f"\n{'=' * 60}")
    print(f"Done! ✅ {success} created, ⏭️ {skip} skipped, ❌ {fail} failed")
    print(f"Total properties defined: {len(PROPERTIES)}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
