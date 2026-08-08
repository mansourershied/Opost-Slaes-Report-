from sqlalchemy import Column, Integer, String
from database import Base


class Business(Base):
    __tablename__ = "businesses"

    # =========================
    # Basic Information
    # =========================
    id = Column(Integer, primary_key=True)

    name = Column(String)
    short_name = Column(String)
    email = Column(String)
    phone = Column(String)
    created_at = Column(String)

    # =========================
    # Business Details
    # =========================
    account_manager = Column(String)
    office = Column(String)
    price_plan = Column(String)
    business_line = Column(String)

    # =========================
    # Calculated Fields
    # =========================
    age = Column(Integer)               # عدد أيام إنشاء الحساب
    incubation = Column(String)         # حضانة / خارج الحضانة

    # =========================
    # Shipment Statistics
    # =========================
    total_shipments = Column(Integer)
    delivered_shipments = Column(Integer)
    returned_shipments = Column(Integer)
    pending_shipments = Column(Integer)

    # =========================
    # Excel Notes
    # =========================
    notes = Column(String)