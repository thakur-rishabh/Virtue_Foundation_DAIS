import streamlit as st
from databricks import sql
import pandas as pd
import os

# Page config
st.set_page_config(
    page_title="Data Readiness Desk",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Databricks connection
@st.cache_resource
def get_db_connection():
    return sql.connect(
        server_hostname=os.getenv("DATABRICKS_SERVER_HOSTNAME"),
        http_path=os.getenv("DATABRICKS_HTTP_PATH"),
        access_token=os.getenv("DATABRICKS_TOKEN")
    )

# Initialize session state
if 'user_id' not in st.session_state:
    st.session_state.user_id = os.getenv("USER", "data_steward")

# Sidebar
with st.sidebar:
    st.title("🏥 Data Readiness Desk")
    st.markdown("---")
    st.info(f"👤 User: {st.session_state.user_id}")
    
    # Quick stats
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT COUNT(*) as total_facilities
        FROM virtue_foundation_silver.healthcare_facilities.facilities_clean
    """)
    total_facilities = cursor.fetchone()[0]
    
    cursor.execute("""
        SELECT COUNT(DISTINCT facility_id) as facilities_with_issues
        FROM virtue_foundation_silver.healthcare_facilities.data_quality_flags
        WHERE resolved_at IS NULL
    """)
    facilities_with_issues = cursor.fetchone()[0]
    
    cursor.execute("""
        SELECT COUNT(*) as total_flags
        FROM virtue_foundation_silver.healthcare_facilities.data_quality_flags
        WHERE resolved_at IS NULL
    """)
    total_flags = cursor.fetchone()[0]
    
    st.metric("Total Facilities", f"{total_facilities\:,}")
    st.metric("Facilities with Issues", f"{facilities_with_issues\:,}")
    st.metric("Open Issues", f"{total_flags\:,}")
    
    cursor.close()

# Main content
st.title("🏥 Healthcare Facility Data Readiness Desk")
st.markdown("""
Welcome to the Data Readiness Desk! This app helps you monitor, validate, and improve 
the quality of healthcare facility data before it powers other applications.

**Navigate using the sidebar** to:
- 📊 View quality metrics and trends
- 🔍 Triage and prioritize issues
- ✏️ Clean and validate data interactively
- 🗺️ Fix missing coordinates
- 📋 Perform bulk operations
- 📈 Track progress and audit changes
""")

# Quick actions
st.markdown("### Quick Actions")
col1, col2, col3 = st.columns(3)

with col1:
    if st.button("🚨 View Critical Issues", use_container_width=True):
        st.switch_page("pages/2_🔍_Issue_Triage.py")

with col2:
    if st.button("✏️ Start Cleaning", use_container_width=True):
        st.switch_page("pages/3_✏️_Data_Cleaning.py")

with col3:
    if st.button("📊 View Dashboard", use_container_width=True):
        st.switch_page("pages/1_📊_Dashboard.py")
Page 1: Dashboard (pages/1_📊_Dashboard.py)
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from utils.db_connector import get_connection

st.set_page_config(page_title="Quality Dashboard", page_icon="📊", layout="wide")

st.title("📊 Data Quality Dashboard")

conn = get_connection()

# Overall quality score
col1, col2, col3, col4 = st.columns(4)

with col1:
    query = """
        SELECT AVG(data_quality_score) as avg_score
        FROM virtue_foundation_silver.healthcare_facilities.facilities_clean
    """
    avg_score = pd.read_sql(query, conn)['avg_score'][0]
    st.metric("Avg Quality Score", f"{avg_score\:.1f}/100")

with col2:
    query = """
        SELECT COUNT(DISTINCT facility_id) as count
        FROM virtue_foundation_silver.healthcare_facilities.data_quality_flags
        WHERE resolved_at IS NULL AND flag_severity = 'critical'
    """
    critical = pd.read_sql(query, conn)['count'][0]
    st.metric("Critical Issues", critical, delta=None, delta_color="inverse")

with col3:
    query = """
        SELECT COUNT(DISTINCT facility_id) as count
        FROM virtue_foundation_silver.healthcare_facilities.data_quality_flags
        WHERE resolved_at IS NULL AND flag_severity = 'high'
    """
    high = pd.read_sql(query, conn)['count'][0]
    st.metric("High Priority", high, delta=None, delta_color="inverse")

with col4:
    query = """
        SELECT COUNT(*) as count
        FROM virtue_foundation_silver.healthcare_facilities.facilities_clean
        WHERE data_quality_score >= 80
    """
    ready = pd.read_sql(query, conn)['count'][0]
    st.metric("Production Ready", ready, delta=None, delta_color="normal")

st.markdown("---")

# Issue breakdown by type
col1, col2 = st.columns(2)

with col1:
    st.subheader("Issues by Type")
    query = """
        SELECT flag_type, COUNT(*) as count
        FROM virtue_foundation_silver.healthcare_facilities.data_quality_flags
        WHERE resolved_at IS NULL
        GROUP BY flag_type
        ORDER BY count DESC
    """
    df = pd.read_sql(query, conn)
    fig = px.bar(df, x='count', y='flag_type', orientation='h',
                 labels={'count': 'Number of Issues', 'flag_type': 'Issue Type'},
                 color='count', color_continuous_scale='Reds')
    st.plotly_chart(fig, use_container_width=True)

with col2:
    st.subheader("Issues by Severity")
    query = """
        SELECT flag_severity, COUNT(*) as count
        FROM virtue_foundation_silver.healthcare_facilities.data_quality_flags
        WHERE resolved_at IS NULL
        GROUP BY flag_severity
        ORDER BY 
            CASE flag_severity 
                WHEN 'critical' THEN 1 
                WHEN 'high' THEN 2 
                WHEN 'medium' THEN 3 
                ELSE 4 
            END
    """
    df = pd.read_sql(query, conn)
    colors = {'critical': '#FF3621', 'high': '#FFAB00', 'medium': '#00A972', 'low': '#919191'}
    fig = px.pie(df, values='count', names='flag_severity',
                 color='flag_severity', color_discrete_map=colors)
    st.plotly_chart(fig, use_container_width=True)

# Geographic distribution
st.subheader("Data Quality by State")
query = """
    SELECT 
        f.address_state,
        COUNT(*) as total_facilities,
        AVG(f.data_quality_score) as avg_quality_score,
        COUNT(DISTINCT q.facility_id) as facilities_with_issues
    FROM virtue_foundation_silver.healthcare_facilities.facilities_clean f
    LEFT JOIN virtue_foundation_silver.healthcare_facilities.data_quality_flags q
        ON f.facility_id = q.facility_id AND q.resolved_at IS NULL
    WHERE f.address_state IS NOT NULL
    GROUP BY f.address_state
    ORDER BY total_facilities DESC
    LIMIT 15
"""
df = pd.read_sql(query, conn)
fig = go.Figure()
fig.add_trace(go.Bar(name='Total Facilities', x=df['address_state'], y=df['total_facilities']))
fig.add_trace(go.Bar(name='With Issues', x=df['address_state'], y=df['facilities_with_issues']))
fig.update_layout(barmode='group', xaxis_title='State', yaxis_title='Count')
st.plotly_chart(fig, use_container_width=True)

# Completeness heatmap
st.subheader("Field Completeness")
query = """
    SELECT 
        ROUND(AVG(CASE WHEN name IS NOT NULL THEN 100 ELSE 0 END), 1) as name,
        ROUND(AVG(CASE WHEN address_city IS NOT NULL THEN 100 ELSE 0 END), 1) as city,
        ROUND(AVG(CASE WHEN latitude IS NOT NULL THEN 100 ELSE 0 END), 1) as coordinates,
        ROUND(AVG(CASE WHEN number_doctors IS NOT NULL THEN 100 ELSE 0 END), 1) as doctors,
        ROUND(AVG(CASE WHEN capacity_beds IS NOT NULL THEN 100 ELSE 0 END), 1) as capacity,
        ROUND(AVG(CASE WHEN email IS NOT NULL THEN 100 ELSE 0 END), 1) as email,
        ROUND(AVG(CASE WHEN official_phone IS NOT NULL THEN 100 ELSE 0 END), 1) as phone
    FROM virtue_foundation_silver.healthcare_facilities.facilities_clean
"""
df = pd.read_sql(query, conn)
st.dataframe(df, use_container_width=True)

conn.close()
Page 2: Issue Triage (pages/2_🔍_Issue_Triage.py)
import streamlit as st
import pandas as pd
from utils.db_connector import get_connection

st.set_page_config(page_title="Issue Triage", page_icon="🔍", layout="wide")

st.title("🔍 Issue Triage & Prioritization")

conn = get_connection()

# Filters
col1, col2, col3 = st.columns(3)

with col1:
    severity_filter = st.multiselect(
        "Severity",
        options=['critical', 'high', 'medium', 'low'],
        default=['critical', 'high']
    )

with col2:
    query = "SELECT DISTINCT flag_type FROM virtue_foundation_silver.healthcare_facilities.data_quality_flags"
    flag_types = pd.read_sql(query, conn)['flag_type'].tolist()
    type_filter = st.multiselect("Issue Type", options=flag_types, default=flag_types[\:3])

with col3:
    query = "SELECT DISTINCT address_state FROM virtue_foundation_silver.healthcare_facilities.facilities_clean WHERE address_state IS NOT NULL"
    states = pd.read_sql(query, conn)['address_state'].tolist()
    state_filter = st.multiselect("State", options=states)

# Build query
where_clauses = []
if severity_filter:
    where_clauses.append(f"q.flag_severity IN ({','.join([f\"'{s}'\" for s in severity_filter])})")
if type_filter:
    where_clauses.append(f"q.flag_type IN ({','.join([f\"'{t}'\" for t in type_filter])})")
if state_filter:
    where_clauses.append(f"f.address_state IN ({','.join([f\"'{s}'\" for s in state_filter])})")

where_clause = " AND " + " AND ".join(where_clauses) if where_clauses else ""

# Fetch issues
query = f"""
    SELECT 
        q.flag_id,
        q.facility_id,
        f.name as facility_name,
        f.address_city,
        f.address_state,
        q.flag_type,
        q.flag_severity,
        q.flag_description,
        q.flagged_at,
        f.data_quality_score
    FROM virtue_foundation_silver.healthcare_facilities.data_quality_flags q
    INNER JOIN virtue_foundation_silver.healthcare_facilities.facilities_clean f
        ON q.facility_id = f.facility_id
    WHERE q.resolved_at IS NULL {where_clause}
    ORDER BY 
        CASE q.flag_severity 
            WHEN 'critical' THEN 1 
            WHEN 'high' THEN 2 
            WHEN 'medium' THEN 3 
            ELSE 4 
        END,
        q.flagged_at DESC
    LIMIT 100
"""

df = pd.read_sql(query, conn)

st.info(f"Found {len(df)} issues matching your filters")

# Display issues
if not df.empty:
    # Color code by severity
    def highlight_severity(row):
        colors = {
            'critical': 'background-color: #ffcccc',
            'high': 'background-color: #ffe6cc',
            'medium': 'background-color: #ffffcc',
            'low': 'background-color: #e6f7ff'
        }
        return [colors].get(row['flag_severity'], '')] * len(row)
    
    styled_df = df.style.apply(highlight_severity, axis=1)
    st.dataframe(styled_df, use_container_width=True, height=600)
    
    # Quick action
    st.markdown("### Quick Actions")
    selected_facility = st.selectbox("Select facility to fix", df['facility_id'].tolist())
    
    if st.button("🔧 Fix This Facility"):
        st.session_state.selected_facility_id = selected_facility
        st.switch_page("pages/3_✏️_Data_Cleaning.py")
else:
    st.success("🎉 No issues found matching your filters!")

conn.close()