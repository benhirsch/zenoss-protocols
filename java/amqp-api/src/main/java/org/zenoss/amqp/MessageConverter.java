/*****************************************************************************
 * 
 * Copyright (C) Zenoss, Inc. 2010-2011, all rights reserved.
 * 
 * This content is made available according to terms specified in
 * License.zenoss under the directory where your Zenoss product is installed.
 * 
 ****************************************************************************/


package org.zenoss.amqp;

/**
 * Interface which can be used to convert a message to/from its raw byte[]
 * encoding for simplifying consumers and producers.
 * 
 * @param <T>
 *            The underlying type of a message body.
 */
public interface MessageConverter<T> {
    /**
     * Converts a message from the raw byte[] representation to a more
     * convenient type. If the message fails encoding or this method returns
     * null, then an exception of type {@link MessageDecoderException} is thrown
     * to the consumer containing the original message which failed to be
     * decoded.
     * 
     * @param bytes
     *            The raw byte[] representation of the message body.
     * @param properties
     *            The properties of the message.
     * @return The decoded message body type.
     * @throws Exception
     *             If the message cannot be converted.
     * @see MessageDecoderException
     */
    public T fromBytes(byte[] bytes, MessageProperties properties)
            throws Exception;

    /**
     * Converts a message from its native type to a byte[] for encoding.
     * 
     * @param message
     *            The message body type.
     * @param propertyBuilder
     *            Message property builder.
     * @return The raw byte[] representation of the message body.
     * @throws Exception
     *             If the message cannot be converted.
     */
    public byte[] toBytes(T message, MessagePropertiesBuilder propertyBuilder)
            throws Exception;
}
