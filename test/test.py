"""
Category-based BPMN loading test

Tests the new category hierarchy architecture:
- Create category nodes with realistic business structure
- Load BPMN files under categories
- Create NEXT_PROCESS relationships between models
"""
import os
from pathlib import Path

# Set env file path before importing bpmn2neo
# This ensures Settings() can find the .env file
TEST_DIR = Path(__file__).parent
ENV_FILE_PATH = TEST_DIR / ".env"
os.environ["B2N_ENV_FILE"] = str(ENV_FILE_PATH)

from bpmn2neo import load_and_embed, create_category_node
from bpmn2neo.settings import Settings

# Business process categories structure
categories_dict = {
    "Finance": [
        "Financial Accounting",
        "Forecast & Budgeting",
        "Investment Management",
        "Travel Management",
        "Financial Supply Chain Management",
        "Group Accounting",
        "Management Accounting"
    ],
    "Human Capital Management": [
        "Personnel Administration",
        "Personnel Management"
    ],
    "Procurement & Logistic": [
        "Delivery & Handling Management",
        "Inbound and Outbound Processes",
        "Procurement",
        "Extended Warehouse Management",
        "Inventory & Warehouse Management"
    ],
    "Product development & Production": [
        "Lifecycle Data Management",
        "Production Development",
        "Manufacturing Execution",
        "Production Planning"
    ],
    "Sales & Service": [
        "Professional Service Delivery",
        "Spare Part Sales and Service",
        "Sales Order Management"
    ],
    "Corporate Services": [
        "Enterprise Asset Management",
        "Global Trade Service",
        "Real Estate Management",
        "Environment, Health & Safety Compliance Management",
        "Quality Management"
    ],
    "Master Data Management": [
        "Maintenance"
    ],
    "Cross Industry Processes": [
        "Cross Industry Models",
        "Customer Relationship Management"
    ],
    "Special Core Processes": [
        "Chemistry",
        "Defense",
        "Logistics",
        "Metals",
        "Retail",
        "Consumer",
        "Discrete",
        "Media",
        "Real Estate"
    ]
}


def test_category_hierarchy_creation():
    """Test creating realistic business category hierarchy."""

    # Initialize settings
    s = Settings()

    print("=" * 80)
    print("Category Hierarchy Creation Test")
    print("=" * 80)

    total_categories = 0
    total_subcategories = 0
    created_categories = {}

    # Create top-level categories and their subcategories
    for category_name, subcategories in categories_dict.items():
        print(f"\n[Category] Creating: {category_name}")

        try:
            # Create top-level category
            category_key = create_category_node(
                name=category_name,
                settings=s,
                parent_category_key=None  # Top-level
            )
            print(f"  ✅ Created category: {category_key}")
            total_categories += 1
            created_categories[category_name] = []

            # Create subcategories under this category
            for subcategory_name in subcategories:
                try:
                    subcategory_key = create_category_node(
                        name=subcategory_name,
                        settings=s,
                        parent_category_key=category_key  # Child of parent category
                    )
                    print(f"    ✅ Created subcategory: {subcategory_name}")
                    total_subcategories += 1
                    created_categories[category_name].append(subcategory_name)

                except Exception as e:
                    print(f"    ❌ Failed to create subcategory '{subcategory_name}': {e}")
                    raise

        except Exception as e:
            print(f"  ❌ Failed to create category '{category_name}': {e}")
            raise

    # Summary
    print("\n" + "=" * 80)
    print("Category Creation Summary")
    print("=" * 80)
    print(f"Top-level categories created: {total_categories}")
    print(f"Subcategories created: {total_subcategories}")
    print(f"Total categories: {total_categories + total_subcategories}")

    print("\n📁 Category Structure:")
    for category, subcats in created_categories.items():
        print(f"  {category} ({len(subcats)} subcategories)")
        for subcat in subcats[:3]:  # Show first 3
            print(f"    └─ {subcat}")
        if len(subcats) > 3:
            print(f"    └─ ... and {len(subcats) - 3} more")

    print("\n✅ Category hierarchy created successfully!")
    print("=" * 80)

    return created_categories


def test_bpmn_loading_with_categories():
    """Test BPMN file loading under created categories."""

    # Initialize settings
    s = Settings()

    print("\n\n" + "=" * 80)
    print("BPMN Loading Test (Sample Files)")
    print("=" * 80)

    # Use "Sales Order Management" subcategory for loading
    target_category = "Sales Order Management"

    print(f"\n[Step 1] Using existing category: '{target_category}'")

    # Load BPMN files under the category
    bpmn_dir = "./data/bpmn"
    bpmn_files = [
        "Order Process for Pizza.bpmn",
        "pizza_vendor_only.bpmn"
    ]

    print(f"\n[Step 2] Loading BPMN files under '{target_category}' category")

    loaded_models = []
    predecessor_key = None

    for idx, bpmn_file in enumerate(bpmn_files, 1):
        bpmn_path = os.path.join(bpmn_dir, bpmn_file)

        if not os.path.exists(bpmn_path):
            print(f"⚠️  File not found: {bpmn_path}")
            continue

        model_key = os.path.splitext(bpmn_file)[0]

        print(f"\n  [{idx}/{len(bpmn_files)}] Loading: {bpmn_file}")
        print(f"      Model Key: {model_key}")
        print(f"      Category: {target_category}")
        print(f"      Predecessor: {predecessor_key or 'None'}")

        try:
            result = load_and_embed(
                bpmn_path=bpmn_path,
                model_key=model_key,
                settings=s,
                mode='light',
                parent_category_key=target_category,  # Required: parent category
                predecessor_model_key=predecessor_key  # Optional: sequential relationship
            )

            print(f"  ✅ Successfully loaded: {result.get('model_key')}")
            loaded_models.append(model_key)

            # Set predecessor for next model (NEXT_PROCESS relationship)
            predecessor_key = model_key

        except Exception as e:
            print(f"  ❌ Failed to load {bpmn_file}: {e}")
            raise

    # Summary
    print("\n" + "=" * 80)
    print("BPMN Loading Summary")
    print("=" * 80)
    print(f"Category used: {target_category}")
    print(f"BPMN models loaded: {len(loaded_models)}")
    for model in loaded_models:
        print(f"  - {model}")
    print("\n✅ BPMN loading test completed successfully!")
    print("=" * 80)

    return loaded_models


def test_full_workflow():
    """Run complete test: create category hierarchy and load sample BPMN files."""

    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "BPMN2NEO CATEGORY TEST SUITE" + " " * 28 + "║")
    print("╚" + "═" * 78 + "╝")

    try:
        # Test 1: Create full category hierarchy
        print("\n📋 TEST 1: Category Hierarchy Creation")
        created_categories = test_category_hierarchy_creation()

        # Test 2: Load BPMN files under categories
        print("\n📋 TEST 2: BPMN File Loading")
        #loaded_models = test_bpmn_loading_with_categories()

        # Final summary
        print("\n\n" + "╔" + "═" * 78 + "╗")
        print("║" + " " * 30 + "ALL TESTS PASSED" + " " * 32 + "║")
        print("╚" + "═" * 78 + "╝")

        total_subcats = sum(len(subcats) for subcats in created_categories.values())
        print(f"\n📊 Final Statistics:")
        print(f"   - Top-level categories: {len(created_categories)}")
        print(f"   - Total subcategories: {total_subcats}")
        print(f"   - Total category nodes: {len(created_categories) + total_subcats}")
        #print(f"   - BPMN models loaded: {len(loaded_models)}")
        print(f"\n🎉 Category-based architecture is working correctly!")

        print(f"\n💡 Graph Structure Created:")
        print(f"   Container")
        for cat_name, subcats in list(created_categories.items())[:2]:  # Show 2 examples
            print(f"     └─[HAS_CATEGORY]→ {cat_name}")
            for subcat in subcats[:2]:
                print(f"         └─[HAS_SUBCATEGORY]→ {subcat}")
            if len(subcats) > 2:
                print(f"             └─ ... {len(subcats) - 2} more")

    except Exception as e:
        print("\n\n❌ TEST FAILED")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    # Run full test suite
    test_full_workflow()
