"""Recover explicitly marked commercial DOM cards before lossy text reduction."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)
    start: int = 0
    end: int = 0
    parent: Node | None = field(default=None, repr=False)

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def text(self, *, skip_options=False):
        if self.tag in {'script', 'style', 'svg', 'noscript'}:
            return ''
        cls = self.attrs.get('class', '')
        if skip_options and ('m-tariff__options' in cls or 'additional-services_' in cls or 'catalog-service-card__linked-products' in cls):
            return ''
        if skip_options and 'universal-regulator__label' in cls:
            markers = [node for node in self.nodes() if 'mm-web-radio__marker' in node.attrs.get('class', '').split()]
            # Access-speed alternatives remain advertised tariff speeds; the
            # existing contract reports their maximum. TV/SMS choices are defaults.
            speed_option = bool(re.search(r'\d+(?:[.,]\d+)?\s*(?:мбит|гбит)', self.text(), re.I))
            if markers and not speed_option and not any('mm-web-radio__marker_selected' in node.attrs.get('class', '').split() for node in markers):
                return ''
        parts = [child.text(skip_options=skip_options) if isinstance(child, Node) else child
                 for child in self.children]
        value = ''.join(parts)
        return f'\n{value}\n' if self.tag in {'div', 'p', 'br', 'span', 'li', 'a'} else value


class Parser(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, payload):
        super().__init__(convert_charrefs=True)
        self.payload = payload
        self.offsets = [0]
        for match in re.finditer('\n', payload):
            self.offsets.append(match.end())
        self.root = Node('root', {})
        self.stack = [self.root]

    def position(self):
        row, col = self.getpos()
        return self.offsets[row - 1] + col

    def handle_starttag(self, tag, attrs):
        start = self.position()
        node = Node(tag, dict((key, value or '') for key, value in attrs), start=start,
                    end=start + len(self.get_starttag_text()), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, 0, -1) if self.stack[i].tag == tag), None)
        if index is None:
            return
        end = self.payload.find('>', self.position()) + 1
        for node in self.stack[index:]:
            node.end = end
        del self.stack[index:]

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def explicit_dom_offer_blocks(payload: str) -> list[str]:
    # These are explicit tariff/price-card markers, not arbitrary containers
    # containing a price. Most provider pages retain their existing extraction.
    if not re.search(r'(?:rates-list-internet__item|card__wrapper|m-tariff__|mkdf-tours-standard-item|catalog-service-card__|class=["\'][^"\']*dopprice|itemtype=["\']https?://schema.org/Product|class=["\'][^"\']*general-cont)', payload):
        return []
    from .html_reducer import _clean_lines

    parser = Parser(payload)
    parser.feed(payload)
    all_nodes = list(parser.root.nodes())
    def is_heading(item):
        classes = item.attrs.get('class', '').split()
        return 'nametp' in classes or ('szha' in classes and 'head-black' in classes)

    heading_nodes = [item for item in all_nodes if is_heading(item)]
    price_nodes = [item for item in all_nodes if 'prc' in item.attrs.get('class', '').split()]
    blocks = []
    for node in all_nodes:
        cls = node.attrs.get('class', '').split()
        if not (node.attrs.get('itemtype', '').rstrip('/').endswith('/Product')
                or is_heading(node) or 'm-tariff' in cls or 'mkdf-tours-standard-item' in cls
                or 'catalog-service-card' in cls
                or 'card__wrapper' in cls
                or 'rates-list-internet__item' in cls
                or ('general-cont' in cls and node.attrs.get('itemtype', '').endswith('/Offer'))):
            continue
        descendants = list(node.nodes())
        if 'rates-list-internet__item' in cls:
            title = next((item for item in descendants if 'rates-list-internet__item-title' in item.attrs.get('class', '').split()), None)
            current = next((item for item in descendants if 'rates-list-internet__item-price' in item.attrs.get('class', '').split()), None)
            if title is None or current is None:
                continue
            name = re.sub(r'\s+', ' ', title.text()).strip()
            rendered = '\n'.join(_clean_lines(node.text()))
            price = re.sub(r'\s+', ' ', current.text()).strip()
            if re.search(r'\d+\s*(?:мбит|гбит)', rendered, re.I) and re.search(r'₽\s*/\s*мес', price, re.I):
                optional = [re.sub(r'\s+', ' ', item.text()).strip() for item in descendants
                            if 'rates-list-internet__item-proposal' in item.attrs.get('class', '').split()
                            and re.search(r'аренда|роутер', item.text(), re.I)]
                blocks.append('\n'.join(['[DOM CARD TITLE]', name, price, rendered] +
                                        ['Можно добавить: ' + value for value in optional]))
        elif 'card__wrapper' in cls:
            # Universal commercial cards must be recovered BEFORE lossy reduction.
            # Each wrapper supplies its own name, access speed and billing footer.
            if any(item is not node and 'card__wrapper' in item.attrs.get('class', '').split() for item in descendants):
                continue
            title = next((item for item in descendants if 'card-title' in item.attrs.get('class', '').split()), None)
            current = next((item for item in descendants if 'price-main' in item.attrs.get('class', '').split()), None)
            old = next((item for item in descendants if 'price-sale' in item.attrs.get('class', '').split()), None)
            if title is None or current is None:
                continue
            name = re.sub(r'\s+', ' ', title.text()).strip()
            rendered = '\n'.join(_clean_lines(node.text(skip_options=True)))
            price = re.sub(r'\s+', ' ', current.text()).strip()
            if not name or not re.search(r'\d+(?:[.,]\d+)?\s*(?:мбит|гбит)', rendered, re.I):
                continue
            if not re.search(r'\d[\d\s]*\s*₽\s*/\s*мес', price, re.I):
                continue
            prefix = ['[DOM CARD TITLE]', name]
            if old is not None:
                prefix.extend(['Акционный тариф', re.sub(r'\s+', ' ', old.text()).strip()])
            prefix.append(price)
            blocks.append('\n'.join(prefix + [rendered]))
        elif 'catalog-service-card' in cls:
            title = next((item for item in descendants if 'catalog-service-card__name' in item.attrs.get('class', '').split()), None)
            footer = next((item for item in descendants if 'catalog-service-card__footer' in item.attrs.get('class', '').split()), None)
            if title is None or footer is None:
                continue
            name = re.sub(r'\s+', ' ', title.text()).strip()
            rendered = '\n'.join(_clean_lines(node.text(skip_options=True)))
            if not re.search(r'\d+\s*(?:мбит|гбит)', rendered, re.I):
                continue
            current = next((item for item in footer.nodes() if 'catalog-price__value' in item.attrs.get('class', '').split()), None)
            old = next((item for item in footer.nodes() if 'catalog-price__original' in item.attrs.get('class', '').split()), None)
            prefix = [name]
            if old is not None:
                prefix.extend(['Акционный тариф', re.sub(r'\s+', ' ', old.text()).strip()])
            if current is not None:
                prefix.append(re.sub(r'\s+', ' ', current.text()).strip())
            if node.parent is not None:
                badges = [item for item in node.parent.nodes() if 'catalog-carousel-badge__title' in item.attrs.get('class', '').split()]
                prefix.extend(re.sub(r'\s+', ' ', item.text()).strip() for item in badges)
            options = [re.sub(r'\s+', ' ', item.text()).strip() for item in descendants if 'catalog-service-card__linked-product' in item.attrs.get('class', '').split()]
            if options:
                prefix.append('Можно добавить: ' + ', '.join(options))
            blocks.append('\n'.join(prefix + [rendered]))
        elif 'general-cont' in cls and node.attrs.get('itemtype', '').endswith('/Offer'):
            title = next((item for item in descendants if 'name-title' in item.attrs.get('class', '').split()), None)
            if title is None:
                continue
            name = re.sub(r'\s+', ' ', title.text()).strip()
            rendered = '\n'.join(_clean_lines(node.text()))
            if re.search(r'\d+\s*(?:мбит|гбит)', rendered, re.I) and re.search(r'\d+\s*руб\./(?:мес|год)', rendered, re.I):
                blocks.append(name + '\n' + rendered)
        elif node.attrs.get('itemtype', '').rstrip('/').endswith('/Product'):
            # Only leaf products are offers. A page-level Product may wrap several
            # products and must never mix their properties with its first card.
            if any(item is not node and item.attrs.get('itemtype', '').rstrip('/').endswith('/Product') for item in descendants):
                continue
            metas = [item for item in descendants if item.tag == 'meta']
            name = next((item.attrs.get('content') for item in metas if item.attrs.get('itemprop') == 'name'), None)
            price = next((item.attrs.get('content') for item in metas if item.attrs.get('itemprop') == 'price'), None)
            properties = {}
            for prop in descendants:
                if prop.attrs.get('itemprop') != 'additionalProperty':
                    continue
                values = {item.attrs.get('itemprop'): item.attrs.get('content') for item in prop.nodes() if item.tag == 'meta'}
                properties[values.get('name')] = values.get('value')
            speed = properties.get('internetSpeed')
            if not name or not price or not speed:
                continue
            visible = next((item for item in descendants if 'rate-card_card' in item.attrs.get('class', '')), None)
            if visible is None:
                continue
            rendered = '\n'.join(_clean_lines(visible.text(skip_options=True)))
            # Monthly period must also be present in the visible commercial card.
            if not re.search(r'(?:/|в\s+)?\s*мес', rendered, re.I):
                continue
            prefix = [name, f'{speed} Мбит/с']
            if properties.get('tvChannelCount'):
                prefix.append(f"{properties['tvChannelCount']} ТВ-каналов")
            if properties.get('mobileIncluded'):
                mobile = properties['mobileIncluded']
                # Common schema sentinel for unlimited traffic is not a literal
                # ten-terabyte allowance in the visible commercial card.
                mobile = re.sub(r'\b10000\s+Гб\b', 'Безлимитный мобильный интернет', mobile, flags=re.I)
                prefix.append(mobile)
            options = [re.sub(r'\s+', ' ', item.text()).strip() for item in visible.nodes()
                       if 'additional-services_wrapperService' in item.attrs.get('class', '')]
            options = list(dict.fromkeys(value for value in options if value))
            if options:
                prefix.append('Можно добавить: ' + ', '.join(options))
            blocks.append('\n'.join(prefix + [rendered]))
        elif 'mkdf-tours-standard-item' in cls:
            title = next((item for item in descendants if 'mkdf-tour-title' in item.attrs.get('class', '').split()), None)
            if title is None:
                continue
            name = re.sub(r'\s+', ' ', title.text()).strip()
            rendered = '\n'.join(_clean_lines(node.text()))
            if not re.search(r'\d+(?:[.,]\d+)?\s*(?:мбит|гбит|mbps|gbps)', rendered, re.I):
                continue
            discounted = any('line-through' in item.attrs.get('style', '') or 'price-with-discount' in item.attrs.get('class', '') for item in descendants)
            prefix = [name]
            if discounted:
                prefix.append('Акционный тариф')
            price_items = [item for item in descendants if 'mkdf-tours-item-price' in item.attrs.get('class', '').split()]
            old = next((item for item in price_items if 'line-through' in item.attrs.get('style', '')), None)
            current = next((item for item in price_items if 'line-through' not in item.attrs.get('style', '') and 'discount-price' not in item.attrs.get('class', '')), None)
            if old is not None and current is not None:
                prefix.extend(re.sub(r'\s+', ' ', item.text()).strip() for item in (old, current))
            blocks.append('\n'.join(prefix + [rendered]))
        elif is_heading(node):
            name_node = node
            next_heading = next((item.start for item in heading_nodes if item.start > node.start), node.start + 9000)
            lexical_prices = [item for item in price_nodes if node.start < item.start < min(next_heading, node.start + 9000)]
            price_node = lexical_prices[0] if len(lexical_prices) == 1 else None
            ancestor = node.parent
            for _ in range(12):
                if price_node is not None:
                    break
                if ancestor is None:
                    break
                nodes = list(ancestor.nodes())
                names = [item for item in nodes if is_heading(item)]
                prices = [item for item in nodes if 'prc' in item.attrs.get('class', '').split()]
                if len(names) > 1:
                    break
                if len(prices) == 1:
                    price_node = prices[0]
                    break
                ancestor = ancestor.parent
            if price_node is None:
                continue
            name = re.sub(r'\s+', ' ', name_node.text()).strip()
            rendered_price = re.sub(r'\s+', ' ', price_node.text()).strip()
            rendered_price = re.sub(r'руб\.\s*_+\s*мес\.', 'руб./мес', rendered_price, flags=re.I)
            if re.search(r'\d+\s*руб\./мес', rendered_price, re.I):
                blocks.append(f'{name}\n{rendered_price}')
        elif 'm-tariff' in cls:
            name_node = next((item for item in descendants if 'm-tariff__name' in item.attrs.get('class', '').split()), None)
            price_node = next((item for item in descendants if 'm-tariff__price-value' in item.attrs.get('class', '').split()), None)
            speed_node = next((item for item in descendants if 'm-tariff__speed' in item.attrs.get('class', '').split()), None)
            description = next((item for item in descendants if 'm-tariff__description' in item.attrs.get('class', '').split()), None)
            if name_node is None or price_node is None or speed_node is None:
                continue
            name = re.sub(r'\s+', ' ', name_node.text()).strip()
            body = '\n'.join(_clean_lines(description.text())) if description else ''
            options = [re.sub(r'\s+', ' ', item.text()).strip() for item in descendants
                       if item.tag == 'label' and item.attrs.get('for', '').startswith('tariff_option_')]
            if options:
                body += '\nМожно добавить: ' + ', '.join(dict.fromkeys(options))
            speed = re.sub(r'\s+', ' ', speed_node.text()).strip()
            value = price_node.attrs.get('data-cost')
            if not value or not re.fullmatch(r'\d+(?:[.,]\d+)?', value):
                continue
            if re.search(r'рублей\s+в\s+месяц', body, re.I):
                blocks.append(f'{name}\n{speed}\n{body}')
            else:
                source = payload[price_node.start:price_node.end]
                blocks.append(f'{name}\n{speed}\n{body}\n[UNSPECIFIED BILLING]\nЦена тарифа: {value} ₽\n[SOURCE PRICE] {source}')
    return list(dict.fromkeys(blocks))
