"""What Bianque says, in Spanish and Portuguese, filled only with verified facts.

Replies are templates, not model output: every amount, date, case or block number in a reply
comes from a tool result the agent verified, so a reply cannot state something that did not
happen. The language model only understands the customer (bianque.agent.llm).
"""

from __future__ import annotations

PRODUCTS = {
    "es": {
        "Tarjeta Crédito": "tarjeta de crédito",
        "Tarjeta Débito": "tarjeta de débito",
        "Cuenta Ahorro": "cuenta de ahorros",
        "Cuenta Corriente": "cuenta corriente",
    },
    "pt": {
        "Tarjeta Crédito": "cartão de crédito",
        "Tarjeta Débito": "cartão de débito",
        "Cuenta Ahorro": "conta poupança",
        "Cuenta Corriente": "conta corrente",
    },
}

TEMPLATES = {
    "es": {
        "alert": "Hola. Vimos un cargo de USD {amount} en {merchant} el {date} con tu {product}. "
        "¿Lo reconoces? Responde 1 si fuiste tú o 2 si no lo reconoces.",
        "login_required": "Para proteger tu cuenta necesito que inicies sesión en la app del "
        "banco. Cuando lo hagas, seguimos desde aquí.",
        "clarify_recognition": "No me quedó claro. ¿Reconoces el cargo de USD {amount} en "
        "{merchant}? Responde 1 si fuiste tú o 2 si no lo reconoces.",
        "clarify_block": "¿Quieres que bloquee provisionalmente tu {product}? Responde sí o no.",
        "clarify_charge": "Para encontrar el cargo, dime el monto aproximado y la fecha.",
        "unsupported": "Solo puedo ayudarte con cargos que no reconoces. Para otras "
        "consultas, escríbenos por los canales de atención del banco.",
        "choose_charge": "Encontré varios cargos que coinciden:\n{options}\n¿Cuál es? "
        "Responde con el número.",
        "ask_recognition": "Encontré el cargo de USD {amount} en {merchant} el {date}. "
        "¿Lo reconoces? Responde 1 si fuiste tú o 2 si no lo reconoces.",
        "ask_block": "Abrí el caso {case_id} para el cargo de USD {amount}. ¿Quieres que "
        "bloquee provisionalmente tu {product} para evitar más cargos? Responde sí o no.",
        "done_blocked": "Listo. Tu caso {case_id} está abierto y tu {product} quedó bloqueada "
        "provisionalmente (bloqueo {block_id}). Te avisaremos cuando se resuelva.",
        "done_not_blocked": "Listo. Tu caso {case_id} está abierto; no bloqueamos tu "
        "{product}. Te avisaremos cuando se resuelva.",
        "legit_closed": "Gracias por confirmar. Cerramos la alerta del cargo de USD {amount}; "
        "no necesitas hacer nada más.",
        "handoff": "Te comunico con un especialista del banco. Ya tiene el contexto de tu caso"
        "{case_note}, así que no tendrás que repetir nada.",
        "not_found": "No encontré ese cargo en tu cuenta.",
        "closed": "Este caso ya está cerrado. Si necesitas algo más, escríbenos de nuevo.",
        "handed_off": "Tu caso ya está con un especialista del banco; te contactará pronto.",
    },
    "pt": {
        "alert": "Olá. Vimos uma cobrança de USD {amount} em {merchant} no dia {date} no seu "
        "{product}. Você reconhece? Responda 1 se foi você ou 2 se não reconhece.",
        "login_required": "Para proteger sua conta, preciso que você entre no app do banco. "
        "Depois disso, continuamos daqui.",
        "clarify_recognition": "Não ficou claro. Você reconhece a cobrança de USD {amount} em "
        "{merchant}? Responda 1 se foi você ou 2 se não reconhece.",
        "clarify_block": "Você quer que eu bloqueie provisoriamente o seu {product}? "
        "Responda sim ou não.",
        "clarify_charge": "Para encontrar a cobrança, me diga o valor aproximado e a data.",
        "unsupported": "Só posso ajudar com cobranças que você não reconhece. Para outros "
        "assuntos, fale com os canais de atendimento do banco.",
        "choose_charge": "Encontrei várias cobranças que coincidem:\n{options}\nQual é? "
        "Responda com o número.",
        "ask_recognition": "Encontrei a cobrança de USD {amount} em {merchant} no dia {date}. "
        "Você reconhece? Responda 1 se foi você ou 2 se não reconhece.",
        "ask_block": "Abri o caso {case_id} para a cobrança de USD {amount}. Você quer que eu "
        "bloqueie provisoriamente o seu {product} para evitar novas cobranças? "
        "Responda sim ou não.",
        "done_blocked": "Pronto. Seu caso {case_id} está aberto e o seu {product} foi "
        "bloqueado provisoriamente (bloqueio {block_id}). Avisaremos quando for resolvido.",
        "done_not_blocked": "Pronto. Seu caso {case_id} está aberto; não bloqueamos o seu "
        "{product}. Avisaremos quando for resolvido.",
        "legit_closed": "Obrigado por confirmar. Encerramos o alerta da cobrança de USD "
        "{amount}; você não precisa fazer mais nada.",
        "handoff": "Vou transferir você para um especialista do banco. Ele já tem o contexto "
        "do seu caso{case_note}, então você não precisará repetir nada.",
        "not_found": "Não encontrei essa cobrança na sua conta.",
        "closed": "Este caso já está encerrado. Se precisar de algo mais, escreva de novo.",
        "handed_off": "Seu caso já está com um especialista do banco; ele entrará em contato "
        "em breve.",
    },
}


def product_name(product_type: str, language: str) -> str:
    return PRODUCTS[language].get(product_type, product_type.lower())


def render(key: str, language: str, **facts: object) -> str:
    language = language if language in TEMPLATES else "es"
    return TEMPLATES[language][key].format(**facts)


def charge_facts(charge, language: str) -> dict:
    """Template fields from a verified ChargeView."""
    return {
        "amount": f"{charge.amount_usd:,.2f}",
        "merchant": charge.merchant or ("el comercio" if language == "es" else "o comércio"),
        "date": charge.transaction_date[:10],
        "product": product_name(charge.product_type, language),
    }
