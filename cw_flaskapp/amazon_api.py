import urllib2
import random
import unicodedata
from time import sleep
import logging
import os
from datetime import datetime

from lxml import objectify
import bottlenose
from bs4 import BeautifulSoup

from config import get_logger
from amazonconfig import AMAZON_KEY_ID, AMAZON_SECRET_KEY, AMAZON_AFFILIATE_ID

logger = get_logger('amazon_api')


# allow us to print lxml.objectify objects in a nice way
# can pull this out in prod
# objectify.enable_recursive_str()


def api_error_handler(err):
    ex = err['exception']
    url = err['api_url']
    logger.debug('%s error getting %s ', type(ex), url)
    if isinstance(ex, urllib2.HTTPError) and ex.code == 503:
        logger.info('hit rate limit on API... waiting...')
        sleep(random.expovariate(0.1))
        return True
    return False


def gracefully_degrade_to_ascii(text):
    ''' Make sure any text return can be handled by a Python string
    '''
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore')


def debug_print_lxml(to_print):
    with open('debug.txt', 'w') as f:
        f.write(str(to_print))


def get_amazon_api():
    ''' Create a bottlenose.Amazon object with the Amazon API credentials
        pulled from the amazonconfig.py file
    '''
    amazon_api = bottlenose.Amazon(AMAZON_KEY_ID, AMAZON_SECRET_KEY, AMAZON_AFFILIATE_ID, MaxQPS=0.9, ErrorHandler=api_error_handler)
    logger.info('Created new API object successfully')
    return amazon_api


def clean_response(response):
    ''' takes a response string
    returns that string without the 'http://webservices.amazon.com/AWSECommerceService/2011-08-01' text

    helps for using tags later on

    '''
    cleaned = response.replace('http://webservices.amazon.com/AWSECommerceService/2011-08-01', '')
    return cleaned


def get_parent_ASIN(ASIN, amazon_api=None):
    ''' input: Amazon ASIN and optional amazon object
       output: the ASIN of the parent or the AuthorityTitle for a book
               if the object doesn't have a parent (or is the parent), the same ASIN is returned
    '''
    if amazon_api is None:
        amazon_api = get_amazon_api()

    # different product types handle related items differently, so we need to
    # know how to handle this product
    product_group = get_product_group(ASIN=ASIN, amazon_api=amazon_api)

    parent_ASIN = ''

    if product_group == 'Book':
        response = clean_response(amazon_api.ItemLookup(ItemId=ASIN, ResponseGroup="RelatedItems,ItemAttributes", Condition='All', RelationshipType='AuthorityTitle'))
        logger.info('API Call to get parent for {0}'.format(ASIN))
        item = objectify.fromstring(response)
        if hasattr(item.Items.Item, 'RelatedItems'):
            if item.Items.Item.RelatedItems.RelatedItem.Item.ItemAttributes.ProductGroup == 'Authority Non Buyable':
                parent_ASIN = item.Items.Item.RelatedItems.RelatedItem.Item.ASIN
            else:
                parent_ASIN = ASIN
        else:
            parent_ASIN = ASIN
    else:
        response = clean_response(amazon_api.ItemLookup(ItemId=ASIN, ResponseGroup="Variations", Condition='All'))
        logger.info('API Call to get parent for {0}'.format(ASIN))
        item = objectify.fromstring(response)

        try:
            parent_ASIN = item.Items.Item.ParentASIN
        except:
            parent_ASIN = ASIN

    parent_ASIN = str(parent_ASIN)

    return parent_ASIN


def get_book_variations_from_page(ASIN, amazon_api=None, page_number=1):
    ''' takes an ASIN and a page number
        returns a list of all variation ASINs on that page
    '''
    if amazon_api is None:
        amazon_api = get_amazon_api()

    response = clean_response(amazon_api.ItemLookup(ItemId=ASIN,
                                                    ResponseGroup="RelatedItems",
                                                    Condition='All',
                                                    RelationshipType='AuthorityTitle',
                                                    RelatedItemPage=page_number))

    logger.info('API Call to get variations on {0} (on page {1}'.format(ASIN, str(page_number)))
    root = objectify.fromstring(response)
    
    variations_on_page = []
    if hasattr(root.Items.Item, 'RelatedItems'):
        relatedItems = root.Items.Item.RelatedItems

        for x in relatedItems.iterchildren(tag='RelatedItem'):
            # make sure ASINs are 10 digits
            variationASIN = str(x.Item.ASIN).zfill(10)
            variations_on_page.append(variationASIN)

    return variations_on_page


def get_item_variations_from_parent(parentASIN, amazon_api=None):
    ''' take an amazon "parent" ASIN
        returns a list of item variation ASINs
    '''
    if amazon_api is None:
        amazon_api = get_amazon_api()

    # different product types handle related items differently, so we need to
    # know how to handle this product
    product_group = get_product_group(ASIN=parentASIN, amazon_api=amazon_api)

    if product_group in ('Book', 'Authority Non Buyable'):
        response = clean_response(amazon_api.ItemLookup(ItemId=parentASIN,
                                                        ResponseGroup="RelatedItems",
                                                        Condition='All',
                                                        RelationshipType='AuthorityTitle'))
        logger.info('API Call to get variations from parent for {0}'.format(parentASIN))
        root = objectify.fromstring(response)
        if hasattr(root.Items.Item, 'RelatedItems'):
            relatedItems = root.Items.Item.RelatedItems
            numberOfPages = relatedItems.RelatedItemPageCount
            variationASINs = []
            for i in range(1, numberOfPages + 1):
                variationASINs.extend(get_book_variations_from_page(ASIN=parentASIN, 
                                                                    page_number=i,
                                                                    amazon_api=amazon_api
                                                                    ))
        else:
            variationASINs = [parentASIN]

    else:
        response = clean_response(amazon_api.ItemLookup(ItemId=parentASIN,
                                                        ResponseGroup="Variations",
                                                        Condition='All'))
        logger.info('API Call to get variations from parent for {0}'.format(parentASIN))
        root = objectify.fromstring(response)

        variationASINs = []
        # if root.Items.Item.Variations doesn't exist,
        # there are no variations, so just the one version
        # which we add to the array and return.

        try:
            variations = root.Items.Item.Variations
            for v in variations.iterchildren(tag='Item'):
                varASIN = str(v.parentASIN).zfill(10)
                variationASINs.append(varASIN)
        except:
            variationASINs = [parentASIN]

    return variationASINs


def get_product_group(ASIN, amazon_api=None):
    ''' takes an ASIN
       returns the product group (Book, UnboxVideo, Kitchen, etc)
    '''
    if amazon_api is None:
        amazon_api = get_amazon_api()

    response = clean_response(amazon_api.ItemLookup(ItemId=ASIN, ResponseGroup="ItemAttributes", Condition='All'))
    logger.info('API Call to get product group for {0}'.format(ASIN))
    item = objectify.fromstring(response)

    try:
        product_group = item.Items.Item.ItemAttributes.ProductGroup
    except:
        product_group = 'Not Found'
    finally:
        return product_group


def get_offers(item, amazon_api=None):
    ''' take an item
        get all offers, return a list of dicts of offer info
    '''
    if amazon_api is None:
        amazon_api = get_amazon_api()

    ASIN = item.ASIN
    item_id = item.id

    offers = []

    # first get the main offer - this is the one that "won the Buy Box"

    buybox_response = clean_response(amazon_api.ItemLookup(ItemId=ASIN, ResponseGroup="OfferListings"))
    logger.info('       API Call to get buybox for {0}'.format(ASIN))
    buybox_root = objectify.fromstring(buybox_response)

    if hasattr(buybox_root.Items, 'Item') and buybox_root.Items.Item.Offers.TotalOffers != 0:
        logger.info('        We do have a buybox for ASIN {0}, name {1}'.format(item.ASIN, item.name))
        buybox_condition = buybox_root.Items.Item.Offers.Offer.OfferAttributes.Condition
        buybox_price_amount = buybox_root.Items.Item.Offers.Offer.OfferListing.Price.Amount
        buybox_price_formatted = buybox_root.Items.Item.Offers.Offer.OfferListing.Price.FormattedPrice
        if hasattr(buybox_root.Items.Item.Offers.Offer.OfferListing, 'Availability'):
            buybox_availability = buybox_root.Items.Item.Offers.Offer.OfferListing.Availability
        else:
            buybox_availability = 'Not sure!'
        if buybox_root.Items.Item.Offers.Offer.OfferListing.IsEligibleForPrime == 1:
            buybox_prime_eligible = True
        else:
            buybox_prime_eligible = False

        offers.append({'condition': buybox_condition,
                       'offer_source': 'Buybox',
                       'offer_price_amount': buybox_price_amount,
                       'offer_price_formatted': buybox_price_formatted,
                       'prime_eligible': buybox_prime_eligible,
                       'availability': buybox_availability,
                       'item_id': item_id
                      })
    else:
        logger.info('       No buybox for ASIN {0}, name {1}'.format(item.ASIN, item.name))

    
    logger.info('   After buybox, ASIN {0} ({1}) has {2} offers'.format(item.ASIN, item.name, str(len(offers))))
    # then get the best third-party offers

    tp_response = clean_response(amazon_api.ItemLookup(ItemId=ASIN, ResponseGroup="Offers", Condition='All'))
    logger.info('   API Call to get third-party offers for {0}'.format(ASIN))
    tp_root = objectify.fromstring(tp_response)
    if hasattr(tp_root.Items, 'Item') and hasattr(tp_root.Items.Item, 'Offers'):
        offerList = tp_root.Items.Item.Offers.iterchildren(tag='Offer')

        for o in offerList:
            condition = o.OfferAttributes.Condition
            offer_price_amount = o.OfferListing.Price.Amount
            offer_price_formatted = o.OfferListing.Price.FormattedPrice
            if o.OfferListing.IsEligibleForPrime == 1:
                prime_eligible = True
            else:
                prime_eligible = False
            if hasattr(o.OfferListing, 'Availability'):
                availability = o.OfferListing.Availability
            else:
                availability = 'Not sure!'

            offer = {'condition': condition,
                    'offer_source': 'Other Sellers',
                    'offer_price_amount': offer_price_amount,
                    'offer_price_formatted': offer_price_formatted,
                    'prime_eligible': prime_eligible,
                    'availability': availability,
                    'item_id': item_id
                    }
            offers.append(offer)

        logger.info('   After others, ASIN {0} ({1}) has {2} offers'.format(item.ASIN, item.name, str(len(offers))))

    return offers


def get_images(ASIN, amazon_api=None):
    ''' take an ASIN
        return a dict with the Small, Medium, and Large image URLS and dimensions
        if there's an error, return an empty dict
    '''
    if amazon_api is None:
        amazon_api = get_amazon_api()

    try:
        response = clean_response(amazon_api.ItemLookup(ItemId=ASIN, ResponseGroup="Images", Condition='All'))
        logger.info('API Call to get images for {0}'.format(ASIN))
    except Exception as err:
        logger.error('Error getting images!')
        logger.error(err.msg)
        return {}

    root = objectify.fromstring(response)

    # check for an error element, return {}
    if hasattr(root.Items.Request, 'Errors'):
        logger.error('  Error in the returned Amazon data: '+ root.Items.Request.Errors.__dict__)
        return {}

    images = {}

    if hasattr(root.Items, 'Item'):
        item = root.Items.Item

        smallImage = hasattr(item, 'SmallImage')
        mediumImage = hasattr(item, 'MediumImage')
        largeImage = hasattr(item, 'LargeImage')

        # don't always get all three images, so populate what we have
        ## TODO: combine this code with the get_images_sizes() method

        if smallImage:
            images['SmallImage'] = {"URL": item.SmallImage.URL,
                                    "Height": item.SmallImage.Height,
                                    "Width": item.SmallImage.Width
                                }
        if mediumImage:
            images['MediumImage'] = {"URL": item.MediumImage.URL,
                                    "Height": item.MediumImage.Height,
                                    "Width": item.MediumImage.Width
                                    }
        if largeImage:
            images['MediumImage'] = {"URL": item.LargeImage.URL,
                                    "Height": item.LargeImage.Height,
                                    "Width": item.LargeImage.Width
                                    }

    return images


def check_for_valid_ASIN(ASIN, amazon_api=None):

    if amazon_api is None:
        amazon_api = get_amazon_api()

    response = clean_response(amazon_api.ItemLookup(ItemId=ASIN, ResponseGroup="ItemAttributes"))
    logger.info('API Call to validate ASIN for {0}'.format(ASIN))
    root = objectify.fromstring(response)

    # if there are errors, check for the invalid param one

    if hasattr(root.Items.Request, 'Errors') and root.Items.Request.Errors.Error.Code == 'AWS.InvalidParameterValue':
        return False
    else:
        return True


def get_item_attributes(ASIN, amazon_api=None):
    ''' take an ASIN and a amazon API instance, return a dictionary of the item's attributes
        if there's an error (some items can't be gotten through the API), return an empty dict
    '''
 
    if amazon_api is None:
        amazon_api = get_amazon_api()

    response = clean_response(amazon_api.ItemLookup(ItemId=ASIN, ResponseGroup="ItemAttributes, BrowseNodes"))
    logger.info('API Call to get item attributes for {0}'.format(ASIN))
    root = objectify.fromstring(response)

    # create a BS4 soup to get the ancestors easily 
    # really this should probably all be done in bs4 vs lxml.objectify, but here we are.
    soup = BeautifulSoup(response, 'lxml')

    if soup.ancestors:
        all_ancestornames = soup.ancestors.findAll('name')
        cookbooks_in_ancestry = [x for x in all_ancestornames if x.text == 'Cookbooks, Food & Wine']
        if cookbooks_in_ancestry:
            is_cookbook = True
        else:
            is_cookbook = False
    else:
        is_cookbook = False

    # check for an error element, return {}
    if hasattr(root.Items.Request, 'Errors'):
        ## TODO: This needs better error handling
        return {}

    item_attributes = ''
    if hasattr(root.Items, 'Item'):
        item = root.Items.Item

        URL = ''
        listPriceAmount = ''
        listPriceFormatted = ''
        title = ''
        product_group = ''

        if hasattr(item, 'DetailPageURL'):
            URL = str(item.DetailPageURL)
        if hasattr(item.ItemAttributes, 'ListPrice'):
            listPriceAmount = str(item.ItemAttributes.ListPrice.Amount)
            listPriceFormatted = str(item.ItemAttributes.ListPrice.FormattedPrice)
        if hasattr(item.ItemAttributes, 'Title'):
            title = str(item.ItemAttributes.Title.text.encode('ascii', errors='ignore'))  # had some titles with non-breaking spaces in them. Annoying.
        if hasattr(item.ItemAttributes, 'ProductGroup'):
            product_group = str(item.ItemAttributes.ProductGroup)

        item_attributes = {"URL": URL,
                        "listPriceAmount": listPriceAmount,
                        "listPriceFormatted": listPriceFormatted,
                        "title": title,
                        "product_group": product_group,
                        "is_cookbook": is_cookbook
                        }

    return item_attributes
