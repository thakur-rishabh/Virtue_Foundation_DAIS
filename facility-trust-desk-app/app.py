import uuid
from datetime import datetime

import pandas as pd
import streamlit as st

try:
    from pyspark.sql import SparkSession
    from pyspark.sql import functions as F
    from pyspark.sql.types import StructType, StructField, StringType, FloatType, TimestampType
except Exception:
    SparkSession = None
    F = None


st.set_page_config(page_title="Facility Trust Desk", page_icon="🏥", layout="wide")

CATALOG = "virtue_foundation_silver"
SCHEMA = "healthcare_facilities"
APP_SCHEMA = "facility_trust_desk"
FACILITIES_TABLE = f"{CATALOG}.{SCHEMA}.facilities_clean"
EQUIPMENT_TABLE = f"{CATALOG}.{SCHEMA}.facility_equipment"
SPECIALTIES_TABLE = f"{CATALOG}.{SCHEMA}.facility_specialties"
USER_NOTES_TABLE = f"{CATALOG}.{APP_SCHEMA}.user_notes"
OVERRIDES_TABLE = f"{CATALOG}.{APP_SCHEMA}.trust_score_overrides"
SHORTLISTS_TABLE = f"{CATALOG}.{APP_SCHEMA}.shortlists"
PAGE_SIZE = 20


DEMO_FACILITIES = [
    {
        "facility_id": "F001",
        "name": "Aarogya Care Hospital",
        "address_city": "Pune",
        "address_state": "Maharashtra",
        "capacity_beds": 220,
        "number_doctors": 45,
        "phone_numbers": ["+91-20-4000-1111"],
        "email": "info@aarogyacare.in",
        "official_website": "www.aarogyacare.in",
        "recency_of_page_update": "2026-04-10",
        "follower_count": 7800,
        "post_count": 260,
        "description": "Multi-specialty hospital with trauma and ICU support",
    },
    {
        "facility_id": "F002",
        "name": "Sanjeevani Clinic",
        "address_city": "Lucknow",
        "address_state": "Uttar Pradesh",
        "capacity_beds": 30,
        "number_doctors": 8,
        "phone_numbers": ["+91-522-600-9000"],
        "email": None,
        "official_website": None,
        "recency_of_page_update": None,
        "follower_count": 300,
        "post_count": 12,
        "description": "Community clinic focused on preventive care",
    },
    {
        "facility_id": "F003",
        "name": "Nava Jeevan Medical Center",
        "address_city": "Bengaluru",
        "address_state": "Karnataka",
        "capacity_beds": 120,
        "number_doctors": 32,
        "phone_numbers": [],
        "email": "contact@navajeevan.org",
        "official_website": "www.navajeevan.org",
        "recency_of_page_update": "2026-03-22",
        "follower_count": 4200,
        "post_count": 120,
        "description": "Specialized center for women and child health",
    },
]

DEMO_EQUIPMENT = {
    "F001": ["MRI", "CT Scanner", "Ventilator", "Ultrasound", "X-Ray"],
    "F002": ["X-Ray", "ECG"],
    "F003": ["NICU Incubator", "Ultrasound", "Ventilator"],
}

DEMO_SPECIALTIES = {
    "F001": ["Cardiology", "Neurology", "Orthopedics", "Oncology"],
    "F002": ["General Medicine", "Pediatrics"],
    "F003": ["Obstetrics", "Pediatrics", "Neonatology"],
}


def get_current_user():
    """Get current user email from SQL connection or default."""
    if hasattr(st.session_state, 'sql_conn') and st.session_state.sql_conn is not None:
        try:
            cursor = st.session_state.sql_conn.cursor()
            cursor.execute("SELECT current_user() as user")
            result = cursor.fetchone()
            cursor.close()
            if result:
                return result[0]
        except Exception:
            pass
    return "user@example.com"


def init_state():
    if "selected_facility" not in st.session_state:
        st.session_state.selected_facility = None
    if "page" not in st.session_state:
        st.session_state.page = "search"
    if "search_results" not in st.session_state:
        st.session_state.search_results = None
    if "total_results" not in st.session_state:
        st.session_state.total_results = 0
    if "current_page" not in st.session_state:
        st.session_state.current_page = 1
    if "user_email" not in st.session_state:
        st.session_state.user_email = get_current_user()
    if "spark" not in st.session_state:
        st.session_state.spark = None
        if SparkSession is not None:
            try:
                st.session_state.spark = SparkSession.builder.getOrCreate()
            except Exception:
                st.session_state.spark = None


def calculate_trust_score(facility):
    score = 0.0

    fields = [
        "capacity_beds",
        "number_doctors",
        "equipment_count",
        "specialty_count",
        "phone_numbers",
        "email",
    ]
    filled = sum(1 for f in fields if facility.get(f) not in [None, "", [], "null", "None"])
    score += (filled / len(fields)) * 4.0

    verification = 0.0
    if facility.get("official_website"):
        verification += 0.4
    phones = facility.get("phone_numbers", [])
    if isinstance(phones, list) and len(phones) > 0:
        verification += 0.3
    if facility.get("recency_of_page_update"):
        verification += 0.3
    score += verification * 3.0

    consistency = 1.0
    desc = str(facility.get("description", "")).lower()
    capacity = facility.get("capacity_beds", 0)
    if desc and capacity:
        if ("small" in desc or "clinic" in desc) and capacity > 200:
            consistency -= 0.3
    score += consistency * 2.0

    digital = 0.0
    if facility.get("follower_count"):
        digital += 0.5
    if facility.get("post_count"):
        digital += 0.5
    score += digital * 1.0

    return round(min(10.0, max(0.0, score)), 1)


def get_trust_color(score):
    if score >= 8.0:
        return "🟢", "HIGH"
    if score >= 5.0:
        return "🟡", "MODERATE"
    return "🔴", "LOW"


def _use_spark():
    return st.session_state.spark is not None and F is not None


def _attach_demo_counts(rows):
    out = []
    for row in rows:
        fid = row.get("facility_id")
        equipment = DEMO_EQUIPMENT.get(fid, [])
        specialties = DEMO_SPECIALTIES.get(fid, [])
        row["equipment_count"] = len(equipment)
        row["specialty_count"] = len(specialties)
        out.append(row)
    return out


# ==================== USER NOTES FUNCTIONS ====================
def save_user_note(facility_id, note_text):
    """Save a user note to the Delta table."""
    if not _use_spark():
        st.warning("Notes require Spark to persist. Note not saved.")
        return False
    
    try:
        spark = st.session_state.spark
        note_id = str(uuid.uuid4())
        now = datetime.now()
        
        note_data = [(
            note_id,
            facility_id,
            st.session_state.user_email,
            note_text,
            now,
            now
        )]
        
        note_df = spark.createDataFrame(note_data, schema=StructType([
            StructField("note_id", StringType(), False),
            StructField("facility_id", StringType(), False),
            StructField("user_email", StringType(), False),
            StructField("note_text", StringType(), False),
            StructField("created_at", TimestampType(), False),
            StructField("updated_at", TimestampType(), False),
        ]))
        
        note_df.write.mode("append").saveAsTable(USER_NOTES_TABLE)
        return True
    except Exception as e:
        st.error(f"Error saving note: {str(e)}")
        return False


def get_facility_notes(facility_id):
    """Get all notes for a facility."""
    if not _use_spark():
        return []
    
    try:
        spark = st.session_state.spark
        notes_df = spark.table(USER_NOTES_TABLE).filter(
            F.col("facility_id") == facility_id
        ).orderBy(F.col("created_at").desc())
        
        notes = notes_df.toPandas().to_dict("records")
        return notes
    except Exception:
        return []


# ==================== TRUST SCORE OVERRIDE FUNCTIONS ====================
def save_trust_score_override(facility_id, original_score, override_score, reason):
    """Save a trust score override to the Delta table."""
    if not _use_spark():
        st.warning("Trust score overrides require Spark to persist. Override not saved.")
        return False
    
    try:
        spark = st.session_state.spark
        override_id = str(uuid.uuid4())
        now = datetime.now()
        
        override_data = [(
            override_id,
            facility_id,
            float(original_score),
            float(override_score),
            reason if reason else None,
            st.session_state.user_email,
            now
        )]
        
        override_df = spark.createDataFrame(override_data, schema=StructType([
            StructField("override_id", StringType(), False),
            StructField("facility_id", StringType(), False),
            StructField("original_score", FloatType(), False),
            StructField("override_score", FloatType(), False),
            StructField("reason", StringType(), True),
            StructField("user_email", StringType(), False),
            StructField("created_at", TimestampType(), False),
        ]))
        
        override_df.write.mode("append").saveAsTable(OVERRIDES_TABLE)
        return True
    except Exception as e:
        st.error(f"Error saving override: {str(e)}")
        return False


def get_trust_score_override(facility_id):
    """Get the most recent trust score override for a facility."""
    if not _use_spark():
        return None
    
    try:
        spark = st.session_state.spark
        override_df = spark.table(OVERRIDES_TABLE).filter(
            F.col("facility_id") == facility_id
        ).orderBy(F.col("created_at").desc()).limit(1)
        
        overrides = override_df.toPandas().to_dict("records")
        return overrides[0] if overrides else None
    except Exception:
        return None


# ==================== SHORTLIST FUNCTIONS ====================
def add_to_shortlist(facility_id, shortlist_name, notes=""):
    """Add a facility to a shortlist."""
    if not _use_spark():
        st.warning("Shortlists require Spark to persist. Facility not added.")
        return False
    
    try:
        spark = st.session_state.spark
        
        # Check if already in this shortlist
        existing = spark.table(SHORTLISTS_TABLE).filter(
            (F.col("facility_id") == facility_id) & 
            (F.col("shortlist_name") == shortlist_name) &
            (F.col("user_email") == st.session_state.user_email)
        )
        
        if existing.count() > 0:
            st.warning(f"Facility already in '{shortlist_name}' shortlist")
            return False
        
        shortlist_id = str(uuid.uuid4())
        now = datetime.now()
        
        shortlist_data = [(
            shortlist_id,
            shortlist_name,
            facility_id,
            st.session_state.user_email,
            now,
            notes if notes else None
        )]
        
        shortlist_df = spark.createDataFrame(shortlist_data, schema=StructType([
            StructField("shortlist_id", StringType(), False),
            StructField("shortlist_name", StringType(), False),
            StructField("facility_id", StringType(), False),
            StructField("user_email", StringType(), False),
            StructField("added_at", TimestampType(), False),
            StructField("notes", StringType(), True),
        ]))
        
        shortlist_df.write.mode("append").saveAsTable(SHORTLISTS_TABLE)
        return True
    except Exception as e:
        st.error(f"Error adding to shortlist: {str(e)}")
        return False


def get_user_shortlists():
    """Get all unique shortlist names for the current user."""
    if not _use_spark():
        return []
    
    try:
        spark = st.session_state.spark
        shortlists_df = spark.table(SHORTLISTS_TABLE).filter(
            F.col("user_email") == st.session_state.user_email
        ).select("shortlist_name").distinct().orderBy("shortlist_name")
        
        return [row["shortlist_name"] for row in shortlists_df.collect()]
    except Exception:
        return []


def get_shortlist_facilities(shortlist_name):
    """Get all facilities in a shortlist."""
    if not _use_spark():
        return []
    
    try:
        spark = st.session_state.spark
        shortlist_df = spark.table(SHORTLISTS_TABLE).filter(
            (F.col("shortlist_name") == shortlist_name) &
            (F.col("user_email") == st.session_state.user_email)
        ).orderBy(F.col("added_at").desc())
        
        shortlist_items = shortlist_df.toPandas().to_dict("records")
        
        # Get facility details for each item
        facilities = []
        for item in shortlist_items:
            facility = get_facility_by_id(item["facility_id"])
            if facility:
                facility["shortlist_notes"] = item.get("notes")
                facility["added_at"] = item.get("added_at")
                facilities.append(facility)
        
        return facilities
    except Exception:
        return []


def remove_from_shortlist(facility_id, shortlist_name):
    """Remove a facility from a shortlist."""
    if not _use_spark():
        return False
    
    try:
        spark = st.session_state.spark
        
        # Read existing data
        all_data = spark.table(SHORTLISTS_TABLE).filter(
            ~((F.col("facility_id") == facility_id) & 
              (F.col("shortlist_name") == shortlist_name) &
              (F.col("user_email") == st.session_state.user_email))
        )
        
        # Overwrite table
        all_data.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(SHORTLISTS_TABLE)
        return True
    except Exception as e:
        st.error(f"Error removing from shortlist: {str(e)}")
        return False


def is_in_shortlist(facility_id, shortlist_name):
    """Check if a facility is in a specific shortlist."""
    if not _use_spark():
        return False
    
    try:
        spark = st.session_state.spark
        count = spark.table(SHORTLISTS_TABLE).filter(
            (F.col("facility_id") == facility_id) & 
            (F.col("shortlist_name") == shortlist_name) &
            (F.col("user_email") == st.session_state.user_email)
        ).count()
        
        return count > 0
    except Exception:
        return False


# ==================== FACILITY SEARCH FUNCTIONS ====================
def search_facilities(search_text="", state_filter=None, specialty_filter=None, has_equipment=False, page=1):
    if _use_spark():
        try:
            spark = st.session_state.spark
            df = spark.table(FACILITIES_TABLE)

            equip_df = (
                spark.table(EQUIPMENT_TABLE)
                .groupBy("facility_id")
                .count()
                .withColumnRenamed("count", "equipment_count")
            )
            df = df.join(equip_df, on="facility_id", how="left").fillna({"equipment_count": 0})

            spec_df = (
                spark.table(SPECIALTIES_TABLE)
                .groupBy("facility_id")
                .count()
                .withColumnRenamed("count", "specialty_count")
            )
            df = df.join(spec_df, on="facility_id", how="left").fillna({"specialty_count": 0})

            if search_text:
                q = search_text.lower()
                df = df.filter(
                    F.lower(F.col("name")).contains(q)
                    | F.lower(F.col("address_city")).contains(q)
                    | F.lower(F.col("address_state")).contains(q)
                )

            if state_filter and state_filter != "All":
                df = df.filter(F.col("address_state") == state_filter)

            if specialty_filter and specialty_filter != "All":
                spec_ids = (
                    spark.table(SPECIALTIES_TABLE)
                    .filter(F.lower(F.col("specialty_name")).contains(specialty_filter.lower()))
                    .select("facility_id")
                    .distinct()
                )
                df = df.join(spec_ids, on="facility_id", how="inner")

            if has_equipment:
                df = df.filter(F.col("equipment_count") > 0)

            total_count = df.count()
            offset = (page - 1) * PAGE_SIZE
            rows = df.orderBy("name").limit(offset + PAGE_SIZE).toPandas()
            if offset > 0:
                rows = rows.iloc[offset: offset + PAGE_SIZE]
            return rows.to_dict("records"), total_count
        except Exception:
            pass

    rows = _attach_demo_counts([dict(x) for x in DEMO_FACILITIES])

    if search_text:
        q = search_text.lower()
        rows = [
            r
            for r in rows
            if q in str(r.get("name", "")).lower()
            or q in str(r.get("address_city", "")).lower()
            or q in str(r.get("address_state", "")).lower()
        ]

    if state_filter and state_filter != "All":
        rows = [r for r in rows if r.get("address_state") == state_filter]

    if specialty_filter and specialty_filter != "All":
        rows = [
            r
            for r in rows
            if any(specialty_filter.lower() in s.lower() for s in DEMO_SPECIALTIES.get(r.get("facility_id"), []))
        ]

    if has_equipment:
        rows = [r for r in rows if r.get("equipment_count", 0) > 0]

    total_count = len(rows)
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    return rows[start:end], total_count


def get_facility_by_id(facility_id):
    if _use_spark():
        try:
            spark = st.session_state.spark
            facility = (
                spark.table(FACILITIES_TABLE)
                .filter(F.col("facility_id") == facility_id)
                .toPandas()
                .to_dict("records")
            )
            if not facility:
                return None

            facility = facility[0]
            equipment = (
                spark.table(EQUIPMENT_TABLE)
                .filter(F.col("facility_id") == facility_id)
                .select("equipment_name")
                .toPandas()
            )
            specialties = (
                spark.table(SPECIALTIES_TABLE)
                .filter(F.col("facility_id") == facility_id)
                .select("specialty_name")
                .toPandas()
            )
            facility["equipment_list"] = equipment["equipment_name"].tolist() if not equipment.empty else []
            facility["specialty_list"] = specialties["specialty_name"].tolist() if not specialties.empty else []
            facility["equipment_count"] = len(facility["equipment_list"])
            facility["specialty_count"] = len(facility["specialty_list"])
            return facility
        except Exception:
            pass

    for row in DEMO_FACILITIES:
        if row["facility_id"] == facility_id:
            out = dict(row)
            out["equipment_list"] = DEMO_EQUIPMENT.get(facility_id, [])
            out["specialty_list"] = DEMO_SPECIALTIES.get(facility_id, [])
            out["equipment_count"] = len(out["equipment_list"])
            out["specialty_count"] = len(out["specialty_list"])
            return out
    return None


def get_all_states():
    if _use_spark():
        try:
            spark = st.session_state.spark
            states = (
                spark.table(FACILITIES_TABLE)
                .select("address_state")
                .distinct()
                .orderBy("address_state")
                .toPandas()
            )
            return ["All"] + [s for s in states["address_state"].dropna().tolist() if s]
        except Exception:
            pass

    states = sorted({r.get("address_state") for r in DEMO_FACILITIES if r.get("address_state")})
    return ["All"] + states


# ==================== RENDER FUNCTIONS ====================
def render_facility_card(facility):
    trust_score = calculate_trust_score(facility)
    
    # Check for override
    override = get_trust_score_override(facility.get("facility_id"))
    if override:
        display_score = override["override_score"]
        color_emoji, confidence = get_trust_color(display_score)
        score_text = f"{display_score}/10 (Override)"
    else:
        display_score = trust_score
        color_emoji, confidence = get_trust_color(display_score)
        score_text = f"{display_score}/10"

    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown(f"### 🏥 {facility.get('name', 'Unknown')}")
        st.markdown(f"📍 {facility.get('address_city', '')}, {facility.get('address_state', '')}")
        st.markdown(
            f"📊 {facility.get('capacity_beds', 'N/A')} beds | {facility.get('number_doctors', 'N/A')} doctors"
        )

    with col2:
        st.markdown(f"### {color_emoji} {score_text}")
        st.markdown(f"**{confidence}**")
        if st.button("View Details", key=f"view_{facility.get('facility_id')}"):
            st.session_state.selected_facility = facility.get("facility_id")
            st.session_state.page = "detail"
            st.rerun()

    st.divider()


def render_search_page():
    # Header with navigation
    col_title, col_nav = st.columns([3, 1])
    with col_title:
        st.title("🏥 FACILITY TRUST DESK")
        st.markdown("Search and assess healthcare facility claims with transparency")
    with col_nav:
        st.write("")
        if st.button("📋 My Shortlists", use_container_width=True):
            st.session_state.page = "shortlists"
            st.rerun()

    if _use_spark():
        st.caption(f"Data source: {FACILITIES_TABLE} with normalized joins")
        st.caption(f"Logged in as: {st.session_state.user_email}")
    else:
        st.info("Spark tables are unavailable in this runtime. Showing demo data with the same UX.")

    col1, col2 = st.columns([4, 1])
    with col1:
        search_text = st.text_input("🔍 Search", placeholder="facility name or city")
    with col2:
        st.write("")
        st.write("")
        search_clicked = st.button("Search", type="primary", use_container_width=True)

    st.markdown("### Filters")
    f1, f2, f3 = st.columns(3)
    with f1:
        state_filter = st.selectbox("State", get_all_states())
    with f2:
        specialty_filter = st.selectbox(
            "Specialty",
            ["All", "Cardiology", "Neurology", "Oncology", "Orthopedics", "Pediatrics"],
        )
    with f3:
        has_equipment = st.checkbox("Has Equipment")

    st.divider()

    if search_clicked or st.session_state.search_results is None:
        with st.spinner("Searching..."):
            results, total = search_facilities(
                search_text=search_text,
                state_filter=state_filter if state_filter != "All" else None,
                specialty_filter=specialty_filter if specialty_filter != "All" else None,
                has_equipment=has_equipment,
                page=st.session_state.current_page,
            )
            st.session_state.search_results = results
            st.session_state.total_results = total

    results = st.session_state.search_results
    total = st.session_state.total_results
    st.markdown(f"### 📋 Results: {total} facilities")

    if not results:
        st.info("No facilities found")
        return

    for fac in results:
        render_facility_card(fac)

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if total_pages > 1:
        p1, p2, p3 = st.columns([1, 2, 1])
        with p1:
            if st.session_state.current_page > 1 and st.button("← Previous"):
                st.session_state.current_page -= 1
                st.session_state.search_results = None
                st.rerun()
        with p2:
            st.markdown(
                f"<center>Page {st.session_state.current_page} of {total_pages}</center>",
                unsafe_allow_html=True,
            )
        with p3:
            if st.session_state.current_page < total_pages and st.button("Next →"):
                st.session_state.current_page += 1
                st.session_state.search_results = None
                st.rerun()


def render_detail_page():
    if st.button("← Back to Search"):
        st.session_state.page = "search"
        st.rerun()

    facility = get_facility_by_id(st.session_state.selected_facility)
    if not facility:
        st.error("Facility not found")
        return

    st.title(f"🏥 {facility.get('name', 'Unknown')}")
    st.markdown(f"📍 {facility.get('address_city', '')}, {facility.get('address_state', '')}")

    c1, c2, c3 = st.columns(3)
    with c1:
        phones = facility.get("phone_numbers", [])
        if phones:
            st.markdown(f"📞 {', '.join([str(p) for p in phones[:2]])}")
    with c2:
        if facility.get("email"):
            st.markdown(f"📧 {facility.get('email')}")
    with c3:
        if facility.get("official_website"):
            st.markdown(f"🌐 [Website](http://{facility.get('official_website')})")

    st.divider()

    # ==================== TRUST ASSESSMENT ====================
    st.markdown("## TRUST ASSESSMENT")
    trust_score = calculate_trust_score(facility)
    override = get_trust_score_override(facility.get("facility_id"))
    
    if override:
        display_score = override["override_score"]
        color, conf = get_trust_color(display_score)
        
        s1, s2 = st.columns([1, 3])
        with s1:
            st.markdown(f"# {color} {display_score}/10")
            st.markdown(f"**{conf} CONFIDENCE**")
            st.caption(f"Original: {trust_score}/10")
        with s2:
            st.markdown("### Override Active")
            st.info(f"**Reason:** {override.get('reason', 'No reason provided')}")
            st.caption(f"Overridden by {override.get('user_email')} on {override.get('created_at')}")
    else:
        display_score = trust_score
        color, conf = get_trust_color(display_score)
        
        s1, s2 = st.columns([1, 3])
        with s1:
            st.markdown(f"# {color} {display_score}/10")
            st.markdown(f"**{conf} CONFIDENCE**")
        with s2:
            st.markdown("### Breakdown")
            st.markdown("✓ Data Completeness: 40%")
            st.markdown("⚠️ Verification: 30%")
            st.markdown("✓ Consistency: 20%")
            st.markdown("📱 Digital: 10%")

    # Override Score Section
    with st.expander("🎯 Override Trust Score"):
        st.markdown("Manually adjust the trust score based on external verification")
        
        ov_col1, ov_col2 = st.columns([2, 1])
        with ov_col1:
            new_score = st.slider(
                "New Trust Score",
                min_value=0.0,
                max_value=10.0,
                value=float(display_score),
                step=0.1,
                key="override_score_slider"
            )
        with ov_col2:
            st.metric("Current Score", display_score)
        
        reason = st.text_area(
            "Reason for Override",
            placeholder="E.g., Called facility and verified bed count...",
            key="override_reason"
        )
        
        if st.button("💾 Save Override", type="primary"):
            if reason.strip():
                if save_trust_score_override(
                    facility.get("facility_id"),
                    trust_score,
                    new_score,
                    reason
                ):
                    st.success("Trust score override saved!")
                    st.rerun()
            else:
                st.warning("Please provide a reason for the override")

    st.divider()

    # ==================== FACILITY CLAIMS ====================
    st.markdown("## FACILITY CLAIMS")

    with st.expander("🏥 CAPACITY", expanded=True):
        st.markdown(f"**Claim:** {facility.get('capacity_beds', 'N/A')} beds")
        st.markdown(f"**Source:** `{FACILITIES_TABLE}.capacity_beds`")
        st.warning("⚠️ Self-reported, unverified")

    with st.expander("👨‍⚕️ STAFF"):
        st.markdown(f"**Claim:** {facility.get('number_doctors', 'N/A')} doctors")
        st.markdown(f"**Source:** `{FACILITIES_TABLE}.number_doctors`")
        st.warning("⚠️ May include visiting consultants")

    with st.expander("🔬 EQUIPMENT"):
        equip = facility.get("equipment_list", [])
        if equip:
            st.markdown(f"**Equipment:** {', '.join(equip[:5])}")
            if len(equip) > 5:
                st.markdown(f"...and {len(equip) - 5} more")
            st.markdown(f"**Source:** `{EQUIPMENT_TABLE}`")
        else:
            st.info("No equipment data")
        st.warning("⚠️ Equipment may be out of service or under maintenance")

    with st.expander("🏥 SPECIALTIES"):
        specs = facility.get("specialty_list", [])
        if specs:
            st.markdown(f"**Specialties:** {', '.join(specs[:5])}")
            if len(specs) > 5:
                st.markdown(f"...and {len(specs) - 5} more")
            st.markdown(f"**Source:** `{SPECIALTIES_TABLE}`")
        else:
            st.info("No specialty data")
        st.warning("⚠️ Specialties may be claimed but not currently available")

    st.divider()

    # ==================== USER NOTES ====================
    st.markdown("## 📝 USER NOTES & ANNOTATIONS")
    
    # Display existing notes
    notes = get_facility_notes(facility.get("facility_id"))
    if notes:
        st.markdown(f"**{len(notes)} note(s):**")
        for note in notes:
            with st.container():
                st.markdown(f"**{note.get('user_email')}** - {note.get('created_at')}")
                st.markdown(f"> {note.get('note_text')}")
                st.markdown("---")
    else:
        st.info("No notes yet. Add the first note below.")
    
    # Add new note
    with st.expander("✏️ Add New Note"):
        note_text = st.text_area(
            "Your Note",
            placeholder="E.g., Called facility on June 15, 2026. Staff confirmed 340 beds (not 350). Equipment list is accurate.",
            key="new_note_text",
            height=100
        )
        
        if st.button("💾 Save Note", type="primary"):
            if note_text.strip():
                if save_user_note(facility.get("facility_id"), note_text):
                    st.success("Note saved successfully!")
                    st.rerun()
            else:
                st.warning("Please enter a note")

    st.divider()

    # ==================== SHORTLIST MANAGEMENT ====================
    st.markdown("## 📋 SHORTLIST MANAGEMENT")
    
    existing_shortlists = get_user_shortlists()
    
    col_shortlist1, col_shortlist2 = st.columns([3, 1])
    
    with col_shortlist1:
        if existing_shortlists:
            shortlist_name = st.selectbox(
                "Add to Shortlist",
                ["[Create New]"] + existing_shortlists,
                key="shortlist_selector"
            )
            
            if shortlist_name == "[Create New]":
                new_shortlist_name = st.text_input(
                    "New Shortlist Name",
                    placeholder="E.g., High Priority Verification",
                    key="new_shortlist_name"
                )
                shortlist_name = new_shortlist_name
        else:
            shortlist_name = st.text_input(
                "Shortlist Name",
                placeholder="E.g., High Priority Verification",
                key="first_shortlist_name"
            )
    
    with col_shortlist2:
        st.write("")
        st.write("")
        if st.button("➕ Add to Shortlist", type="primary", use_container_width=True):
            if shortlist_name and shortlist_name != "[Create New]":
                if add_to_shortlist(facility.get("facility_id"), shortlist_name):
                    st.success(f"Added to '{shortlist_name}'")
                    st.rerun()
            else:
                st.warning("Please enter a shortlist name")
    
    # Show current shortlists
    if existing_shortlists:
        st.markdown("**In Shortlists:**")
        for sl_name in existing_shortlists:
            if is_in_shortlist(facility.get("facility_id"), sl_name):
                col_sl1, col_sl2 = st.columns([4, 1])
                with col_sl1:
                    st.markdown(f"✓ {sl_name}")
                with col_sl2:
                    if st.button("Remove", key=f"remove_{sl_name}"):
                        if remove_from_shortlist(facility.get("facility_id"), sl_name):
                            st.success(f"Removed from '{sl_name}'")
                            st.rerun()


def render_shortlists_page():
    """Render the shortlists management page."""
    if st.button("← Back to Search"):
        st.session_state.page = "search"
        st.rerun()
    
    st.title("📋 MY SHORTLISTS")
    st.markdown("Manage your facility shortlists")
    
    if _use_spark():
        st.caption(f"Logged in as: {st.session_state.user_email}")
    
    st.divider()
    
    shortlists = get_user_shortlists()
    
    if not shortlists:
        st.info("You don't have any shortlists yet. Add facilities to shortlists from their detail pages.")
        return
    
    # Shortlist selector
    selected_shortlist = st.selectbox(
        "Select Shortlist",
        shortlists,
        key="shortlist_viewer"
    )
    
    if selected_shortlist:
        st.markdown(f"### 📁 {selected_shortlist}")
        
        facilities = get_shortlist_facilities(selected_shortlist)
        
        if not facilities:
            st.info("No facilities in this shortlist")
            return
        
        st.markdown(f"**{len(facilities)} facilities**")
        
        for facility in facilities:
            # Render facility card
            trust_score = calculate_trust_score(facility)
            override = get_trust_score_override(facility.get("facility_id"))
            
            if override:
                display_score = override["override_score"]
                score_text = f"{display_score}/10 (Override)"
            else:
                display_score = trust_score
                score_text = f"{display_score}/10"
            
            color_emoji, confidence = get_trust_color(display_score)
            
            with st.container():
                col1, col2, col3 = st.columns([3, 1, 1])
                
                with col1:
                    st.markdown(f"### 🏥 {facility.get('name', 'Unknown')}")
                    st.markdown(f"📍 {facility.get('address_city', '')}, {facility.get('address_state', '')}")
                    st.markdown(f"📊 {facility.get('capacity_beds', 'N/A')} beds | {facility.get('number_doctors', 'N/A')} doctors")
                    if facility.get("shortlist_notes"):
                        st.caption(f"Note: {facility.get('shortlist_notes')}")
                
                with col2:
                    st.markdown(f"### {color_emoji} {score_text}")
                    st.markdown(f"**{confidence}**")
                
                with col3:
                    st.write("")
                    if st.button("View", key=f"view_sl_{facility.get('facility_id')}"):
                        st.session_state.selected_facility = facility.get("facility_id")
                        st.session_state.page = "detail"
                        st.rerun()
                    
                    if st.button("Remove", key=f"remove_sl_{facility.get('facility_id')}"):
                        if remove_from_shortlist(facility.get("facility_id"), selected_shortlist):
                            st.success("Removed from shortlist")
                            st.rerun()
                
                st.divider()


def main():
    init_state()
    
    if st.session_state.page == "search":
        render_search_page()
    elif st.session_state.page == "shortlists":
        render_shortlists_page()
    else:
        render_detail_page()


if __name__ == "__main__":
    main()