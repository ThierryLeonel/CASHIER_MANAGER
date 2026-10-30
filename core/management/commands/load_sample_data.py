"""Load the clearly marked [SAMPLE] organisation, optionally with activity.

    python manage.py load_sample_data                # structure + users only
    python manage.py load_sample_data --activity     # plus a realistic day of postings

Refuses to run if any zone already exists (protects real data)."""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from core.models import ExpenseCategory, Zone
from core.sample import build_sample_org
from core.services import closing as cl
from core.services import operations as ops
from core.services.ledger import balance

D = Decimal


class Command(BaseCommand):
    help = "Create the [SAMPLE] organisation (zones, cashiers, SIMs, agents, POS) for demo/testing."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="Sample-Pass-2026!")
        parser.add_argument("--activity", action="store_true", help="Also post sample transactions")

    def handle(self, *args, **opts):
        if Zone.objects.exists():
            raise CommandError("Zones already exist. Sample data is only loaded into an empty database.")
        o = build_sample_org(opts["password"])
        self.stdout.write(self.style.SUCCESS("Sample organisation created. Users (password: %s):" % opts["password"]))
        for u in ("admin", "manager", "junior.fako", "finance", "cashier.fako1", "cashier.fako2", "cashier.meme", "agent.ndive",
                  "agent.ekane", "shop.molyko", "shop.molyko.om", "shop.molyko.airtime", "shop.senior"):
            self.stdout.write(f"  {u}")
        if opts["activity"]:
            self._activity(o)
            self.stdout.write(self.style.SUCCESS("Sample activity posted."))

    def _activity(self, o):
        admin, f1, f2, me = o["admin"], o["c_f1"], o["c_f2"], o["c_me"]
        a1, a2, a3 = o["agents"]["a1"], o["agents"]["a2"], o["agents"]["a3"]
        p1, p2 = o["pos"]["p1"], o["pos"]["p2"]
        d1, d2, dm = o["drawers"]["f1"], o["drawers"]["f2"], o["drawers"]["me"]
        for acct, amt in [(d1, 2_000_000), (d2, 1_500_000), (dm, 1_200_000), (o["uve_f1"], 5_000_000),
                          (o["uve_f2"], 3_000_000), (o["uve_me"], 2_500_000), (o["er_f1"], 1_000_000),
                          (o["er_me"], 600_000)]:
            ops.record_opening_balance(admin, account=acct, amount=D(amt), description="[SAMPLE] launch balance")
        ops.record_opening_balance(admin, account=a2.account, amount=D(150_000), description="[SAMPLE] owed at launch")
        ops.record_opening_balance(admin, account=o["cm1"].account, amount=D(-50_000),
                                   description="[SAMPLE] company owed cash manager at launch")
        ops.record_supply(f1, product="UVE", customer=a1, amount=D(500_000), source_account=o["uve_f1"])
        ops.record_supply(f1, product="ER", customer=a1, amount=D(120_000), source_account=o["er_f1"])
        ops.record_supply(f1, product="ER", customer=a2, amount=D(80_000), source_account=o["er_f1"])
        ops.record_supply(f2, product="CASH", customer=a2, amount=D(100_000), source_account=d2)
        ops.record_collection(f1, payer=a1, method="CASH", amount=D(300_000), account=d1)
        ops.record_collection(f2, payer=a1, method="MTN", amount=D(150_000), account=o["mtn_f2"])
        ops.record_collection(me, payer=a1, method="BANK", amount=D(100_000), account=o["bank"],
                              bank_reference="SAMPLE-DEP-001")
        ops.record_destockage(f1, pos=p1, agent=a1, amount=D(200_000), drawer=d1)
        ops.record_collection(f1, payer=a1, method="UVE", amount=D(200_000), account=o["uve_f1"])
        s = ops.record_supply(f2, product="UVE", customer=p2, amount=D(60_000), source_account=o["uve_f2"])
        ops.transfer_responsibility(f2, supply=s, new_party=a2, reason="[SAMPLE] agent collected the POS money")
        ops.record_supply(me, product="UVE", customer=a3, amount=D(250_000), source_account=o["uve_me"])
        ops.record_collection(me, payer=a3, method="CASH", amount=D(300_000), account=dm)  # 50,000 supply due
        ops.record_conversion(f1, uve_account=o["uve_f1"], amount=D(100_000))
        ops.record_expense(f1, category=ExpenseCategory.objects.get(code="fuel"), amount=D(15_000), drawer=d1,
                           description="[SAMPLE] motorbike fuel")
        ops.record_expense(me, category=ExpenseCategory.objects.get(code="orange-prefinance"), amount=D(40_000),
                           drawer=dm, description="[SAMPLE] Orange pre-finance")
        ops.record_internal_transfer(f2, source=o["mtn_f2"], destination=o["mtn_f1"], amount=D(100_000),
                                     description="[SAMPLE] MTN float moved to Fako 1")
        ops.record_exchange(f1, cash_manager=o["cm2"], amount=D(400_000), give_kind="CASH",
                            amount_served=D(250_000), external_ref="SAMPLE-CM-01")  # 150,000 still to be served
        # shop: opening capital, then a day of shop activity
        from core.models import ShopService, StockUnit
        from core.services import shops as sh

        shop, smgr = o["shop"], o["shop_mgr"]
        for kind, amt in (("CASH_DRAWER", 300_000), ("UVE", 800_000), ("ER", 150_000)):
            ops.record_opening_balance(admin, account=shop.account(kind), amount=D(amt), description="[SAMPLE] shop launch")
        from core.services import shop_shifts as ss

        b = sh.shop_balances(shop)
        ss.open_shift(smgr, cash=b["CASH_DRAWER"], uve=b["UVE"], er=b["ER"], note="[SAMPLE] till counted")
        ss.open_shift(o["shop_om"])
        ss.open_shift(o["shop_air"])
        sh.receive_stock(o["shop_senior"], shop=shop, item=o["items"]["phone"],
                         serials="356000000000011\n356000000000029\n356000000000037\n356000000000045\n356000000000052")
        sh.receive_stock(o["shop_senior"], shop=shop, item=o["items"]["kit"],
                         serials="OEK-S-0001, OEK-S-0002, OEK-S-0003")
        sh.cash_in(smgr, amount=D(50_000), commission=D(250), client_phone="677123456")
        sh.cash_out(o["shop_om"], amount=D(20_000), commission=D(200))
        sh.airtime(smgr, amount=D(5_000))
        sh.airtime(o["shop_air"], amount=D(10_000), kind="GOOD_DEAL")
        sh.convert(smgr, amount=D(100_000))
        sh.appro(smgr, agent=a1, amount=D(200_000), pay="CASH")
        sh.er_supply(smgr, agent=a2, amount=D(50_000), pay="CREDIT")
        sh.destockage(smgr, agent=a1, amount=D(30_000))
        sh.destockage(smgr, agent=a1, amount=D(20_000), pay="CREDIT")  # the agent owes the shop
        sh.agent_repay(smgr, agent=a1, amount=D(5_000), pay="CASH")
        sh.desk_to_shop(o["c_f1"], shop=shop, amount=D(100_000), description="[SAMPLE] UVE top-up from Fako 1")
        sh.sell(smgr, item=o["items"]["phone"], units=list(StockUnit.objects.filter(serial="356000000000011")), client_name="[SAMPLE] Client Ngwa Peter",
                client_address="Molyko, Buea", client_country="Cameroon", id_type="CNI", id_number="SAMPLE-998877",
                phone="677445566", whatsapp="677445566")
        sh.count_service(smgr, service=ShopService.objects.get(name__startswith="OTT"), count=4)
        sh.retrieve_sim(smgr, sim_number="699887766", holder_name="[SAMPLE] Client Ebai", id_type="CNI",
                        id_number="SAMPLE-1234567")
        # an expense and a purchase of items for resale (bought at cost, one sold later at the catalogue price)
        sh.expense(smgr, category=ExpenseCategory.objects.filter(active=True).first(), amount=D(8_000), pay="CASH",
                   description="[SAMPLE] Electricity October")
        sh.purchase(smgr, item=o["items"]["box"], units=[("356222222222222", "FB-0001"), ("", "FB-0002")],
                    unit_cost=D(22_000), pay="CASH", supplier="[SAMPLE] Orange Cameroun", description="INV-0077")
        fb = list(StockUnit.objects.filter(serial="FB-0001"))
        sh.sell(smgr, units=fb, client_name="[SAMPLE] Client Tabe", client_address="Buea", client_country="Cameroon",
                id_type="CNI", id_number="SAMPLE-55443", phone="677556677")
        # the Orange Money desk leaves first; the manager counts the till at closing (2 000 FCFA cash short)
        ss.close_shift(o["shop_om"])
        ss.close_shift(o["shop_air"])
        b = sh.shop_balances(shop)
        ss.close_shift(smgr, cash=b["CASH_DRAWER"] - D(2000), uve=b["UVE"], er=b["ER"], note="[SAMPLE] closing count")
        # capital: the owners inject cash into Fako 1 and move part of it to the shop
        from core.services import capital as cap

        cap.inject(o["finance"], account=o["drawers"]["f1"], amount=D(1_000_000), source="[SAMPLE] Owner's savings",
                   purpose="Increase the Fako 1 float")
        cap.transfer(o["finance"], source=o["drawers"]["f1"], destination=shop.account("CASH_DRAWER"),
                     amount=D(200_000), purpose="[SAMPLE] Shop Molyko float")
        # Meme closes with a 2,000 cash shortage (left open for investigation)
        actuals = {a.id: balance(a) for a in cl.closing_accounts(me)}
        actuals[dm.id] -= D(2000)
        cl.close_day(me, actuals=actuals, notes="[SAMPLE] closing")
