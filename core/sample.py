"""Sample organisation used by tests and by `manage.py load_sample_data`.

Everything created here is marked [SAMPLE]; SIM numbers are fictitious.
Structure mirrors the requirements: master zones Fako (Fako 1, Fako 2) and
Meme; Fako has 2 Orange business SIMs (one master), Meme has 1 (master);
each cashier has one Orange SIM, one MTN SIM and one drawer. Fako 2 is
granted use of the Fako master ER wallet so it can supply ER (decision #3).
"""
from decimal import Decimal

from django.db import transaction

from core.models import (
    Account, AccountAccess, BankAccount, CashManager, CompanySim, ConversionRate, Counterparty, User, Zone,
)

S = "[SAMPLE] "


def _user(username, role, password, **kw):
    u = User(username=username, role=role, first_name=kw.pop("first_name", username), **kw)
    u.set_password(password)
    u.save()
    return u


@transaction.atomic
def build_sample_org(password="Sample-Pass-2026!"):
    fako = Zone.objects.create(code="fako", name="Fako")
    fako1 = Zone.objects.create(code="fako-1", name="Fako 1", parent=fako)
    fako2 = Zone.objects.create(code="fako-2", name="Fako 2", parent=fako)
    meme = Zone.objects.create(code="meme", name="Meme")
    meme_sub = Zone.objects.create(code="meme-1", name="Meme", parent=meme)

    admin = _user("admin", User.Role.SUPERADMIN, password, first_name=S + "Super admin", is_staff=True)
    manager = _user("manager", User.Role.MANAGER, password, first_name=S + "Manager")
    finance = _user("finance", User.Role.FINANCE, password, first_name=S + "Finance manager")
    c_f1 = _user("cashier.fako1", User.Role.CASHIER, password, first_name=S + "Cashier Fako 1", subzone=fako1)
    c_f2 = _user("cashier.fako2", User.Role.CASHIER, password, first_name=S + "Cashier Fako 2", subzone=fako2)
    c_me = _user("cashier.meme", User.Role.CASHIER, password, first_name=S + "Cashier Meme", subzone=meme_sub)

    sims = {
        "fako_master": CompanySim.objects.create(network="ORANGE", msisdn="690000001", label=S + "Fako master",
                                                 master_zone=fako, is_master=True, custodian=c_f1),
        "fako_2": CompanySim.objects.create(network="ORANGE", msisdn="690000002", label=S + "Fako 2 Orange",
                                            master_zone=fako, custodian=c_f2),
        "meme_master": CompanySim.objects.create(network="ORANGE", msisdn="690000003", label=S + "Meme master",
                                                 master_zone=meme, is_master=True, custodian=c_me),
        "mtn_f1": CompanySim.objects.create(network="MTN", msisdn="670000001", label=S + "MTN Fako 1",
                                            master_zone=fako, custodian=c_f1),
        "mtn_f2": CompanySim.objects.create(network="MTN", msisdn="670000002", label=S + "MTN Fako 2",
                                            master_zone=fako, custodian=c_f2),
        "mtn_me": CompanySim.objects.create(network="MTN", msisdn="670000003", label=S + "MTN Meme",
                                            master_zone=meme, custodian=c_me),
    }
    drawers = {}
    for key, cashier, zone in [("f1", c_f1, fako), ("f2", c_f2, fako), ("me", c_me, meme)]:
        drawers[key] = Account.objects.create(kind=Account.Kind.CASH_DRAWER, name=f"{S}Drawer {cashier.subzone}",
                                              custodian=cashier, zone=zone)
    fako_master_er = Account.objects.get(kind=Account.Kind.ER, sim=sims["fako_master"])
    AccountAccess.objects.create(user=c_f2, account=fako_master_er, granted_by=admin)

    bank = BankAccount.objects.create(bank_name=S + "Afriland First Bank", account_number="SAMPLE-0001",
                                      label="Main account")
    cm1 = CashManager.objects.create(name=S + "Cash manager Buea", phone="650000001")
    cm2 = CashManager.objects.create(name=S + "Cash manager Kumba", phone="650000002")

    agents = {
        "a1": Counterparty.objects.create(kind="AGENT", business_sim="691000001", name=S + "Agent Ndive",
                                          master_zone=fako, created_by=admin),
        "a2": Counterparty.objects.create(kind="AGENT", business_sim="691000002", name=S + "Agent Ekane",
                                          master_zone=fako, created_by=admin),
        "a3": Counterparty.objects.create(kind="AGENT", business_sim="691000003", name=S + "Agent Mbah",
                                          master_zone=meme, created_by=admin),
    }
    pos = {
        "p1": Counterparty.objects.create(kind="POS", business_sim="692000001", name=S + "POS Molyko",
                                          created_by=admin),
        "p2": Counterparty.objects.create(kind="POS", business_sim="692000002", name=S + "POS Kumba market",
                                          created_by=admin),
    }
    agent_user = _user("agent.ndive", User.Role.AGENT, password, first_name=S + "Agent Ndive",
                       counterparty=agents["a1"])
    agent_user2 = _user("agent.ekane", User.Role.AGENT, password, first_name=S + "Agent Ekane",
                        counterparty=agents["a2"])
    ConversionRate.objects.create(uplift_rate=Decimal("0.04"), created_by=admin, note="Initial 4% rate")

    def acct(kind, sim_key):
        return Account.objects.get(kind=kind, sim=sims[sim_key])

    return dict(
        zones=dict(fako=fako, fako1=fako1, fako2=fako2, meme=meme, meme_sub=meme_sub),
        admin=admin, manager=manager, finance=finance, c_f1=c_f1, c_f2=c_f2, c_me=c_me,
        agent_user=agent_user, agent_user2=agent_user2, sims=sims, drawers=drawers, bank=bank.ledger_account,
        cm1=cm1, cm2=cm2, agents=agents, pos=pos,
        uve_f1=acct("UVE", "fako_master"), er_f1=fako_master_er, uve_f2=acct("UVE", "fako_2"),
        er_f2=acct("ER", "fako_2"), uve_me=acct("UVE", "meme_master"), er_me=acct("ER", "meme_master"),
        mtn_f1=acct("MTN", "mtn_f1"), mtn_f2=acct("MTN", "mtn_f2"), mtn_me=acct("MTN", "mtn_me"),
    )
