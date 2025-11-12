import streamlit as st
import pandas as pd
from search_scrape import google_search, scrape_page
from database import save_result, get_results, create_database

create_database()
st.set_page_config(page_title="KidsSmart+ Educational Database", layout="wide")

logo_path = "logo.png"

st.sidebar.image(logo_path, width=150)
st.sidebar.title("📚 KidsSmart+ Educational Database")
page = st.sidebar.radio("Navigate", ["Home", "Download Data"])

if page == "Home":
    st.image(logo_path, width=120)
    st.markdown("<h1 style='text-align:center;font-size:28px;font-weight:bold;'>KidsSmart+ Educational Database</h1>", unsafe_allow_html=True)
    st.write("### 🔍 Search for Educational Data")

    search_query = st.text_input("Enter your search query:")
    if st.button("Search") and search_query:
        with st.spinner("Searching Google…"):
            results = google_search(search_query) or []

        if not results:
            st.warning("No search results found. Try another query.")
        else:
            st.write(f"**Found {len(results)} results. Scraping content…** 🛠️")
            search_data = []
            prog = st.progress(0)

            for i, res in enumerate(results, start=1):
                title, link = res.get("title","(no title)"), res.get("link","")
                try:
                    content = scrape_page(link) or ""
                except Exception as e:
                    content = f"(scrape error: {e})"

                save_result(search_query, title, link, content)
                preview = (content[:300] + "...") if len(content) > 300 else content
                search_data.append({"Title": title, "Link": link, "Content": preview})
                prog.progress(i/len(results))

            st.success("Done! ✅ Data saved to database.")
            st.write("### 🔍 Search Results Preview")
            for item in search_data:
                st.markdown(f"#### 📌 [{item['Title']}]({item['Link']})")
                st.write(item["Content"])
                st.write("---")

elif page == "Download Data":
    st.write("### 📥 Download Stored Data")
    rows = get_results()
    if rows:
        df = pd.DataFrame(rows, columns=["ID","Query","Title","Link","Content"])
        st.dataframe(df, use_container_width=True)
        st.download_button("📥 Download Data as CSV", df.to_csv(index=False), "search_results.csv", "text/csv")
    else:
        st.info("No results stored yet. Perform a search to get data.")

st.markdown("""
<div style="position:fixed;bottom:0;left:0;width:100%;background:#000;color:#fff;text-align:center;font-size:14px;padding:10px;">
Created by <b>Mohamed Imaad Muhinudeen (s8078260)</b> & <b>Kavin Nanthakumar (s8049341)</b> | All Rights Reserved | KidsSmart+
</div>
""", unsafe_allow_html=True)
