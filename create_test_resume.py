"""Creates a realistic base resume for pipeline testing."""
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()

# Name / header
name = doc.add_paragraph()
name.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = name.add_run("Abhishek Saha")
run.bold = True
run.font.size = Pt(18)

contact = doc.add_paragraph()
contact.alignment = WD_ALIGN_PARAGRAPH.CENTER
contact.add_run("Pune, India  |  abhisheksaha@live.com  |  +91-9XXXXXXXXX  |  linkedin.com/in/abhisheksaha")

doc.add_paragraph()

# Summary
h = doc.add_heading("Summary", level=1)
doc.add_paragraph(
    "Results-driven product and business analysis professional with 12+ years of experience "
    "delivering enterprise-grade solutions across BFSI, e-commerce, and SaaS domains. "
    "Proven track record of owning end-to-end product roadmaps and driving cross-functional alignment."
)

# Skills
doc.add_heading("Skills", level=1)
doc.add_paragraph(
    "Product Roadmap | Stakeholder Management | Agile | Scrum | Business Requirements | "
    "Gap Analysis | JIRA | Confluence | SQL | Data Analysis | Power BI | Tableau | "
    "User Story Mapping | Process Improvement | CRM | ERP | Change Management"
)

# Experience
doc.add_heading("Experience", level=1)

doc.add_paragraph("VP – Product Management  |  FinTech Corp, Pune  |  2019 – Present").runs[0].bold = True
doc.add_paragraph(
    "• Owned the product roadmap for a ₹500Cr digital lending platform serving 2M+ customers.\n"
    "• Led cross-functional teams of 40+ engineers, designers, and analysts across 3 geographies.\n"
    "• Reduced time-to-market by 35% by introducing OKR-based sprint planning and KPI dashboards.\n"
    "• Managed P&L of ₹80Cr annual product budget; drove vendor management and governance reviews."
)

doc.add_paragraph("Senior Business Analyst  |  TechSolutions Ltd, Pune  |  2015 – 2019").runs[0].bold = True
doc.add_paragraph(
    "• Authored 60+ BRDs and FRDs for ERP migration and CRM implementation projects.\n"
    "• Facilitated stakeholder workshops with C-suite executives to align on go-to-market strategy.\n"
    "• Delivered data analysis dashboards using Power BI and Tableau for executive reporting."
)

doc.add_paragraph("Business Analyst  |  Infosys, Pune  |  2012 – 2015").runs[0].bold = True
doc.add_paragraph(
    "• Conducted gap analysis and process improvement initiatives for BFSI clients.\n"
    "• Worked on digital transformation projects spanning core banking and payment systems."
)

# Education
doc.add_heading("Education", level=1)
doc.add_paragraph("MBA – Product & Strategy  |  IIM Pune  |  2012")
doc.add_paragraph("B.E. – Computer Science  |  University of Pune  |  2010")

doc.save("resume/base_resume.docx")
print("Created resume/base_resume.docx")
